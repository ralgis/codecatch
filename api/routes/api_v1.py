"""REST API v1 — used by clients (audiotrace-scraper et al.)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

from api.schemas import (
    CodeResponse,
    CodesListResponse,
    CodeWaitRequest,
    ConsumeRequest,
    ConsumeResponse,
    MailboxResponse,
    MailboxUpsertRequest,
    MeResponse,
)
from codecatch.auth import CurrentApiKey, require_admin_scope_key, require_api_key
from codecatch.logging_setup import get_logger
from codecatch.mailbox_service import MailboxError, upsert_mailbox
from codecatch.providers import resolve_provider_by_address

router = APIRouter(prefix="/api/v1")
log = get_logger("api_v1")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─── /me ───────────────────────────────────────────────────────────────────
@router.get("/me", response_model=MeResponse)
async def me(
    request: Request,
    key: Annotated[CurrentApiKey, Depends(require_api_key)],
):
    pool: asyncpg.Pool = request.app.state.db_pool
    slug = await pool.fetchval("SELECT slug FROM tenants WHERE id = $1", key.tenant_id)
    return MeResponse(
        key_name=key.name,
        tenant_id=key.tenant_id,
        tenant_slug=slug,
        is_admin_scope=key.is_admin_scope,
    )


# ─── Mailboxes (admin-scope key required) ─────────────────────────────────
@router.post("/mailboxes", response_model=MailboxResponse, status_code=status.HTTP_200_OK)
async def mailbox_upsert(
    request: Request,
    body: MailboxUpsertRequest,
    key: Annotated[CurrentApiKey, Depends(require_admin_scope_key)],
):
    pool: asyncpg.Pool = request.app.state.db_pool

    provider = await resolve_provider_by_address(pool, body.address)
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_provider",
                "message": f"Domain not in providers catalogue: {body.address.split('@', 1)[1]}",
            },
        )

    try:
        result = await upsert_mailbox(
            pool,
            address=str(body.address),
            tenant_id=key.tenant_id,
            provider_id=provider["id"],
            password=body.password,
            is_group=body.is_group,
            purpose=body.purpose or "",
            notes=body.notes or "",
            proxy_url=body.proxy_url,
            mode=body.mode,
            forwarding_target=body.forwarding_target,
        )
    except MailboxError as e:
        raise HTTPException(
            status_code=400, detail={"error": "validation_error", "message": str(e)}
        ) from e

    row = await pool.fetchrow(
        """
        SELECT m.*, p.name AS provider_name, t.slug AS tenant_slug
        FROM mailboxes m
        LEFT JOIN providers p ON m.provider_id = p.id
        LEFT JOIN tenants t ON m.tenant_id = t.id
        WHERE m.address = $1
        """,
        result.address,
    )
    return MailboxResponse(
        address=row["address"],
        tenant_slug=row["tenant_slug"],
        provider=row["provider_name"],
        status=row["status"],
        is_group=row["is_group"],
        purpose=row["purpose"],
        last_code_at=row["last_code_at"],
        created_at=row["created_at"],
        note=result.note,
        consent_url=row["oauth_consent_url"],
    )


@router.get("/mailboxes/{address}", response_model=MailboxResponse)
async def mailbox_get(
    request: Request,
    address: str,
    key: Annotated[CurrentApiKey, Depends(require_api_key)],
):
    pool: asyncpg.Pool = request.app.state.db_pool
    row = await pool.fetchrow(
        """
        SELECT m.*, p.name AS provider_name, t.slug AS tenant_slug
        FROM mailboxes m
        LEFT JOIN providers p ON m.provider_id = p.id
        LEFT JOIN tenants t ON m.tenant_id = t.id
        WHERE m.address = $1 AND m.tenant_id = $2
        """,
        address.lower(), key.tenant_id,
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Mailbox not registered for your tenant"},
        )
    return MailboxResponse(
        address=row["address"],
        tenant_slug=row["tenant_slug"],
        provider=row["provider_name"],
        status=row["status"],
        is_group=row["is_group"],
        purpose=row["purpose"],
        last_code_at=row["last_code_at"],
        created_at=row["created_at"],
        consent_url=row["oauth_consent_url"],
    )


@router.post("/mailboxes/{address}/setup-forwarding")
async def mailbox_setup_forwarding(
    request: Request,
    address: str,
    key: Annotated[CurrentApiKey, Depends(require_admin_scope_key)],
):
    """Drive Playwright to configure outlook.live.com forwarding for this mailbox.
    Runs synchronously — Playwright session takes 30-90s. Result reflected on
    the mailbox row (forwarding_probe_status='ok'|'failed')."""
    from workers.forwarding_setup import configure_for_mailbox

    pool: asyncpg.Pool = request.app.state.db_pool
    addr = address.lower()
    mb = await pool.fetchrow(
        "SELECT 1 FROM mailboxes WHERE address = $1 AND tenant_id = $2",
        addr, key.tenant_id,
    )
    if not mb:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Mailbox not registered"})
    result = await configure_for_mailbox(pool, addr)
    return {"address": addr, "ok": result.ok, "detail": result.detail}


@router.delete("/mailboxes/{address}", status_code=204)
async def mailbox_delete(
    request: Request,
    address: str,
    key: Annotated[CurrentApiKey, Depends(require_admin_scope_key)],
):
    pool: asyncpg.Pool = request.app.state.db_pool
    result = await pool.execute(
        "DELETE FROM mailboxes WHERE address = $1 AND tenant_id = $2",
        address.lower(), key.tenant_id,
    )
    if result.endswith(" 0"):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Mailbox not found"})
    return Response(status_code=204)


# ─── Codes — long-poll wait ───────────────────────────────────────────────
@router.post("/codes/wait")
async def code_wait(
    request: Request,
    body: CodeWaitRequest,
    key: Annotated[CurrentApiKey, Depends(require_api_key)],
):
    pool: asyncpg.Pool = request.app.state.db_pool
    address = str(body.address).lower()
    since = body.since or (_utcnow().replace(second=0, microsecond=0))
    deadline = asyncio.get_event_loop().time() + body.timeout_sec

    # Step 1: quick check before subscribing
    code = await _find_unconsumed_code(pool, key.tenant_id, address, body.platform, since)
    if code:
        return _code_to_response(code)

    # Step 2: subscribe to pg_notify channel and re-poll on every signal
    channel = f"codes_tenant_{key.tenant_id}"
    notify_event = asyncio.Event()

    async def listen():
        async with pool.acquire() as conn:
            await conn.add_listener(channel, lambda *args: notify_event.set())
            while not notify_event.is_set():
                try:
                    await asyncio.wait_for(asyncio.sleep(3600), timeout=3600)
                except asyncio.TimeoutError:
                    pass

    listen_task = asyncio.create_task(listen())
    try:
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return Response(status_code=204)
            try:
                await asyncio.wait_for(notify_event.wait(), timeout=min(remaining, 5))
            except asyncio.TimeoutError:
                # periodic re-check anyway (in case notification was missed)
                pass
            notify_event.clear()
            code = await _find_unconsumed_code(pool, key.tenant_id, address, body.platform, since)
            if code:
                return _code_to_response(code)
    finally:
        listen_task.cancel()
        try:
            await listen_task
        except (asyncio.CancelledError, Exception):
            pass


async def _find_unconsumed_code(
    pool: asyncpg.Pool,
    tenant_id: int,
    address: str,
    platform: str | None,
    since: datetime,
) -> asyncpg.Record | None:
    query = """
        SELECT c.id, c.target_address, c.code, c.platform, c.sender, c.subject,
               c.body_excerpt, c.source_mailbox, c.received_at, c.consumed_at
        FROM codes c
        WHERE c.tenant_id = $1 AND c.target_address = $2
          AND c.received_at >= $3
          AND c.consumed_at IS NULL
    """
    args: list = [tenant_id, address, since]
    if platform:
        args.append(platform)
        query += f" AND c.platform = ${len(args)}"
    query += " ORDER BY c.received_at ASC LIMIT 1"
    return await pool.fetchrow(query, *args)


def _code_to_response(row: asyncpg.Record) -> JSONResponse:
    return JSONResponse(
        content={
            "code_id": row["id"],
            "target_address": row["target_address"],
            "code": row["code"],
            "platform": row["platform"],
            "sender": row["sender"],
            "subject": row["subject"],
            "body_excerpt": row["body_excerpt"],
            "source_mailbox": row["source_mailbox"],
            "received_at": row["received_at"].isoformat() if row["received_at"] else None,
            "consumed_at": row["consumed_at"].isoformat() if row["consumed_at"] else None,
        }
    )


# ─── Codes — pull list ────────────────────────────────────────────────────
@router.get("/codes", response_model=CodesListResponse)
async def codes_list(
    request: Request,
    key: Annotated[CurrentApiKey, Depends(require_api_key)],
    address: Annotated[str | None, Query()] = None,
    platform: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    consumed: Annotated[str | None, Query(description="'yes' or 'no'")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    pool: asyncpg.Pool = request.app.state.db_pool
    where = ["c.tenant_id = $1"]
    args: list = [key.tenant_id]
    if address:
        args.append(address.lower())
        where.append(f"c.target_address = ${len(args)}")
    if platform:
        args.append(platform)
        where.append(f"c.platform = ${len(args)}")
    if since:
        args.append(since)
        where.append(f"c.received_at >= ${len(args)}")
    if consumed == "yes":
        where.append("c.consumed_at IS NOT NULL")
    elif consumed == "no":
        where.append("c.consumed_at IS NULL")
    args.append(limit)
    rows = await pool.fetch(
        f"""
        SELECT c.id, c.target_address, c.code, c.platform, c.sender, c.subject,
               c.body_excerpt, c.source_mailbox, c.received_at, c.consumed_at
        FROM codes c
        WHERE {" AND ".join(where)}
        ORDER BY c.received_at DESC
        LIMIT ${len(args)}
        """,
        *args,
    )
    items = [
        CodeResponse(
            code_id=r["id"],
            target_address=r["target_address"],
            code=r["code"],
            platform=r["platform"],
            sender=r["sender"],
            subject=r["subject"],
            body_excerpt=r["body_excerpt"],
            source_mailbox=r["source_mailbox"],
            received_at=r["received_at"],
            consumed_at=r["consumed_at"],
        )
        for r in rows
    ]
    return CodesListResponse(count=len(items), items=items)


# ─── Codes — consume ──────────────────────────────────────────────────────
@router.post("/codes/{code_id}/consume", response_model=ConsumeResponse)
async def code_consume(
    request: Request,
    code_id: int,
    body: ConsumeRequest,
    key: Annotated[CurrentApiKey, Depends(require_api_key)],
):
    pool: asyncpg.Pool = request.app.state.db_pool
    row = await pool.fetchrow(
        """
        UPDATE codes
        SET consumed_at = NOW(), consumed_by_key_id = $2, consumed_note = $3
        WHERE id = $1 AND tenant_id = $4 AND consumed_at IS NULL
        RETURNING consumed_at
        """,
        code_id, key.id, body.note, key.tenant_id,
    )
    if not row:
        # Either not found, or already consumed
        exists = await pool.fetchval(
            "SELECT consumed_at FROM codes WHERE id = $1 AND tenant_id = $2",
            code_id, key.tenant_id,
        )
        if exists is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"Code {code_id} not found for your tenant"},
            )
        raise HTTPException(
            status_code=410,
            detail={"error": "gone", "message": "Code already consumed"},
        )
    return ConsumeResponse(
        code_id=code_id, consumed_at=row["consumed_at"], consumed_by=key.name,
    )
