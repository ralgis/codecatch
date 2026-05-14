"""Authentication primitives — session-based for admin UI, API-key for REST.

Session cookie carries the signed admin id. Anything we want to expose in
request-scope (current admin user, current api-key context) goes through a
small set of FastAPI dependency functions; the dependencies query Postgres
on each request — that's fine at our scale and keeps things simple.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import asyncpg
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeSerializer

from codecatch.config import get_settings
from codecatch.crypto import hash_api_key

SESSION_COOKIE = "codecatch_session"
SESSION_SALT = "codecatch.admin-session.v1"


# ─── Dataclasses for request-scope identities ─────────────────────────────
@dataclass(frozen=True)
class CurrentAdmin:
    id: int
    username: str
    is_super_admin: bool
    tenant_id: int | None


@dataclass(frozen=True)
class CurrentApiKey:
    id: int
    name: str
    tenant_id: int
    is_admin_scope: bool


# ─── Session-cookie helpers ───────────────────────────────────────────────
def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt=SESSION_SALT)


def sign_session(admin_id: int) -> str:
    return _serializer().dumps({"admin_id": admin_id})  # type: ignore[no-any-return]


def unsign_session(token: str) -> int | None:
    try:
        data = _serializer().loads(token)
    except BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    aid = data.get("admin_id")
    return aid if isinstance(aid, int) else None


# ─── FastAPI dependencies ─────────────────────────────────────────────────
async def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db_pool  # type: ignore[no-any-return]


async def get_current_admin_optional(
    request: Request,
    session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> CurrentAdmin | None:
    if not session:
        return None
    admin_id = unsign_session(session)
    if admin_id is None:
        return None
    pool = await _pool(request)
    row = await pool.fetchrow(
        "SELECT id, username, is_super_admin, tenant_id, is_active "
        "FROM admins WHERE id = $1",
        admin_id,
    )
    if not row or not row["is_active"]:
        return None
    return CurrentAdmin(
        id=row["id"],
        username=row["username"],
        is_super_admin=row["is_super_admin"],
        tenant_id=row["tenant_id"],
    )


async def require_admin(
    admin: Annotated[CurrentAdmin | None, Depends(get_current_admin_optional)],
) -> CurrentAdmin:
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return admin


async def require_super_admin(
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
) -> CurrentAdmin:
    if not admin.is_super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super-admin required")
    return admin


# ─── API key dependency ───────────────────────────────────────────────────
async def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> CurrentApiKey:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Bearer token required"},
        )
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Empty bearer token"},
        )

    pool = await _pool(request)
    key_hash = hash_api_key(token)
    row = await pool.fetchrow(
        """
        SELECT id, name, tenant_id, is_admin_scope, is_active
        FROM api_keys WHERE key_hash = $1
        """,
        key_hash,
    )
    if not row or not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Invalid or revoked API key"},
        )

    # Touch last_used_at — fire-and-forget, don't slow down request.
    await pool.execute("UPDATE api_keys SET last_used_at = NOW() WHERE id = $1", row["id"])

    return CurrentApiKey(
        id=row["id"],
        name=row["name"],
        tenant_id=row["tenant_id"],
        is_admin_scope=row["is_admin_scope"],
    )


async def require_admin_scope_key(
    key: Annotated[CurrentApiKey, Depends(require_api_key)],
) -> CurrentApiKey:
    if not key.is_admin_scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden", "message": "Admin-scope API key required"},
        )
    return key
