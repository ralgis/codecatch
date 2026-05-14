"""Admin web UI routes.

All routes mounted under /admin require a valid session (see require_admin).
Multi-tenancy: super-admin sees all tenants; tenant-admin sees only their own.
"""
from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from api.templating import templates
from codecatch.audit import write_audit
from codecatch.auth import CurrentAdmin, require_admin, require_super_admin
from codecatch.crypto import (
    decrypt,
    encrypt,
    generate_api_key,
    hash_password,
)

router = APIRouter(prefix="/admin")


# ── tenant scope filter helpers ─────────────────────────────────────────────
def _tenant_filter(admin: CurrentAdmin, table_alias: str = "") -> tuple[str, list[Any]]:
    """Return (where_clause_suffix, params_to_append) for tenant scoping.
    Super-admin sees all; tenant-admin sees only their tenant_id.
    """
    if admin.is_super_admin:
        return "", []
    col = f"{table_alias}.tenant_id" if table_alias else "tenant_id"
    return f" AND {col} = ${{n}}", [admin.tenant_id]


def _q(query: str, args: list[Any], extra_clause: str, extra_args: list[Any]) -> tuple[str, list[Any]]:
    """Substitute ${n} placeholders in extra_clause and append args."""
    n = len(args)
    placed = extra_clause.replace("${n}", f"${n + 1}")
    return query + placed, args + extra_args


# ─── Dashboard ─────────────────────────────────────────────────────────────
@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
):
    pool = request.app.state.db_pool

    base_codes = "SELECT COUNT(*) FROM codes WHERE TRUE"
    args: list[Any] = []
    base_codes, args = _q(base_codes, args, *_tenant_filter(admin))
    total_codes = await pool.fetchval(base_codes, *args)

    base_today = "SELECT COUNT(*) FROM codes WHERE received_at > NOW() - INTERVAL '24 hours'"
    args2: list[Any] = []
    base_today, args2 = _q(base_today, args2, *_tenant_filter(admin))
    codes_today = await pool.fetchval(base_today, *args2)

    base_mb = "SELECT COUNT(*) FROM mailboxes WHERE is_active = TRUE"
    args3: list[Any] = []
    base_mb, args3 = _q(base_mb, args3, *_tenant_filter(admin))
    mailboxes_active = await pool.fetchval(base_mb, *args3)

    n_groups = await pool.fetchval(
        "SELECT COUNT(*) FROM mailboxes WHERE is_group = TRUE AND is_active = TRUE"
    )

    recent_codes_q = (
        "SELECT id, target_address, code, platform, sender, received_at, consumed_at "
        "FROM codes WHERE TRUE"
    )
    args4: list[Any] = []
    recent_codes_q, args4 = _q(recent_codes_q, args4, *_tenant_filter(admin))
    recent_codes_q += " ORDER BY received_at DESC LIMIT 20"
    recent_codes = await pool.fetch(recent_codes_q, *args4)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "admin": admin,
            "stats": {
                "total_codes": total_codes,
                "codes_today": codes_today,
                "mailboxes_active": mailboxes_active,
                "groups_active": n_groups,
            },
            "recent_codes": recent_codes,
        },
    )


# ─── Codes ─────────────────────────────────────────────────────────────────
@router.get("/codes", response_class=HTMLResponse)
async def codes_list(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
    target: str | None = Query(None),
    platform: str | None = Query(None),
    consumed: str | None = Query(None),
    page: int = Query(1, ge=1),
):
    pool = request.app.state.db_pool
    page_size = 50
    offset = (page - 1) * page_size

    where = ["TRUE"]
    args: list[Any] = []
    if target:
        args.append(f"%{target.lower()}%")
        where.append(f"LOWER(target_address) LIKE ${len(args)}")
    if platform:
        args.append(platform)
        where.append(f"platform = ${len(args)}")
    if consumed == "yes":
        where.append("consumed_at IS NOT NULL")
    elif consumed == "no":
        where.append("consumed_at IS NULL")
    if not admin.is_super_admin:
        args.append(admin.tenant_id)
        where.append(f"tenant_id = ${len(args)}")

    where_sql = " AND ".join(where)
    total = await pool.fetchval(f"SELECT COUNT(*) FROM codes WHERE {where_sql}", *args)

    args2 = args + [page_size, offset]
    rows = await pool.fetch(
        f"""
        SELECT id, target_address, code, platform, sender, subject,
               received_at, consumed_at, consumed_note
        FROM codes WHERE {where_sql}
        ORDER BY received_at DESC
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args2,
    )

    return templates.TemplateResponse(
        request,
        "codes.html",
        {
            "admin": admin,
            "codes": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "filters": {"target": target or "", "platform": platform or "", "consumed": consumed or ""},
        },
    )


@router.get("/codes/{code_id}", response_class=HTMLResponse)
async def code_detail(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
    code_id: int,
):
    pool = request.app.state.db_pool
    row = await pool.fetchrow(
        "SELECT * FROM codes WHERE id = $1"
        + ("" if admin.is_super_admin else " AND tenant_id = $2"),
        *([code_id] if admin.is_super_admin else [code_id, admin.tenant_id]),
    )
    if not row:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "code_detail.html", {"admin": admin, "code": row}
    )


@router.post("/codes/{code_id}/consume")
async def code_consume(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
    code_id: int,
):
    pool = request.app.state.db_pool
    await pool.execute(
        """
        UPDATE codes SET consumed_at = NOW(),
            consumed_note = COALESCE(consumed_note, '') || ' (manual via admin ' || $2 || ')'
        WHERE id = $1 AND consumed_at IS NULL
        """ + ("" if admin.is_super_admin else " AND tenant_id = $3"),
        *([code_id, admin.username] if admin.is_super_admin else [code_id, admin.username, admin.tenant_id]),
    )
    return RedirectResponse(url=f"/admin/codes/{code_id}", status_code=303)


# ─── Mailboxes ─────────────────────────────────────────────────────────────
@router.get("/mailboxes", response_class=HTMLResponse)
async def mailboxes_list(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
    is_group: str | None = Query(None),
):
    pool = request.app.state.db_pool
    where = ["m.is_active = TRUE"]
    args: list[Any] = []
    if is_group == "yes":
        where.append("m.is_group = TRUE")
    elif is_group == "no":
        where.append("m.is_group = FALSE")
    if not admin.is_super_admin:
        args.append(admin.tenant_id)
        where.append(f"m.tenant_id = ${len(args)}")

    where_sql = " AND ".join(where)
    rows = await pool.fetch(
        f"""
        SELECT m.address, m.status, m.is_group, m.purpose, m.last_code_at, m.created_at,
               p.name AS provider_name, p.auth_kind AS provider_auth_kind,
               t.slug AS tenant_slug,
               (SELECT COUNT(*) FROM codes c WHERE c.target_address = m.address) AS codes_total
        FROM mailboxes m
        LEFT JOIN providers p ON m.provider_id = p.id
        LEFT JOIN tenants t ON m.tenant_id = t.id
        WHERE {where_sql}
        ORDER BY m.is_group DESC, m.created_at DESC
        """,
        *args,
    )
    return templates.TemplateResponse(
        request, "mailboxes.html", {"admin": admin, "mailboxes": rows, "is_group_filter": is_group}
    )


@router.get("/mailboxes/new", response_class=HTMLResponse)
async def mailbox_new(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
):
    pool = request.app.state.db_pool
    providers = await pool.fetch(
        "SELECT id, name, auth_kind, domain_patterns FROM providers WHERE is_active = TRUE ORDER BY name"
    )
    return templates.TemplateResponse(
        request,
        "mailbox_form.html",
        {"admin": admin, "providers": providers, "mailbox": None, "error": None},
    )


@router.post("/mailboxes/new")
async def mailbox_create(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
    address: Annotated[str, Form()],
    password: Annotated[str, Form()] = "",
    is_group: Annotated[str, Form()] = "",
    purpose: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    pool = request.app.state.db_pool
    from codecatch.providers import resolve_provider_by_address
    from codecatch.mailbox_service import upsert_mailbox

    is_group_bool = is_group in ("on", "yes", "true", "1")
    tenant_id = admin.tenant_id if not admin.is_super_admin else None
    if tenant_id is None:
        # Super admin needs to pick a tenant — for MVP, use 'default'.
        tenant_id = await pool.fetchval("SELECT id FROM tenants WHERE slug = 'default'")

    try:
        provider = await resolve_provider_by_address(pool, address)
        await upsert_mailbox(
            pool,
            address=address,
            tenant_id=tenant_id,
            provider_id=provider["id"] if provider else None,
            password=password or None,
            is_group=is_group_bool,
            purpose=purpose,
            notes=notes,
        )
    except ValueError as e:
        providers = await pool.fetch(
            "SELECT id, name, auth_kind, domain_patterns FROM providers WHERE is_active = TRUE"
        )
        return templates.TemplateResponse(
            request,
            "mailbox_form.html",
            {"admin": admin, "providers": providers, "mailbox": None, "error": str(e)},
            status_code=400,
        )

    return RedirectResponse(url="/admin/mailboxes", status_code=303)


@router.get("/mailboxes/{address}", response_class=HTMLResponse)
async def mailbox_detail(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
    address: str,
):
    pool = request.app.state.db_pool
    where = "m.address = $1"
    args: list[Any] = [address]
    if not admin.is_super_admin:
        args.append(admin.tenant_id)
        where += f" AND m.tenant_id = ${len(args)}"

    mailbox = await pool.fetchrow(
        f"""
        SELECT m.*, p.name AS provider_name, p.auth_kind AS provider_auth_kind,
               t.slug AS tenant_slug
        FROM mailboxes m
        LEFT JOIN providers p ON m.provider_id = p.id
        LEFT JOIN tenants t ON m.tenant_id = t.id
        WHERE {where}
        """,
        *args,
    )
    if not mailbox:
        raise HTTPException(status_code=404)

    passwords = await pool.fetch(
        """
        SELECT id, is_current, verified_at, invalidated_at, invalidation_reason, created_at
        FROM mailbox_passwords WHERE mailbox_address = $1
        ORDER BY created_at DESC LIMIT 50
        """,
        address,
    )
    recent_codes = await pool.fetch(
        """
        SELECT id, code, platform, sender, received_at, consumed_at
        FROM codes WHERE target_address = $1
        ORDER BY received_at DESC LIMIT 20
        """,
        address,
    )

    return templates.TemplateResponse(
        request,
        "mailbox_detail.html",
        {
            "admin": admin,
            "mailbox": mailbox,
            "passwords": passwords,
            "recent_codes": recent_codes,
        },
    )


@router.post("/mailboxes/{address}/delete")
async def mailbox_delete(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
    address: str,
):
    pool = request.app.state.db_pool
    extra = "" if admin.is_super_admin else " AND tenant_id = $2"
    args = [address] if admin.is_super_admin else [address, admin.tenant_id]
    await pool.execute(f"DELETE FROM mailboxes WHERE address = $1{extra}", *args)
    await write_audit(
        pool,
        action="mailbox.delete",
        actor_kind="admin",
        actor_id=admin.username,
        target_kind="mailbox",
        target_id=address,
        success=True,
    )
    return RedirectResponse(url="/admin/mailboxes", status_code=303)


@router.post("/mailboxes/{address}/setup-forwarding")
async def mailbox_setup_forwarding_ui(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
    address: str,
):
    from workers.forwarding_setup import configure_for_mailbox
    pool = request.app.state.db_pool
    result = await configure_for_mailbox(pool, address.lower())
    return {"ok": result.ok, "detail": result.detail}


@router.post("/mailboxes/{address}/reveal-password")
async def mailbox_reveal(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_super_admin)],  # super only
    address: str,
):
    pool = request.app.state.db_pool
    row = await pool.fetchrow(
        """
        SELECT password_encrypted FROM mailbox_passwords
        WHERE mailbox_address = $1 AND is_current = TRUE
        """,
        address,
    )
    if not row:
        raise HTTPException(status_code=404, detail="No stored password")
    plaintext = decrypt(row["password_encrypted"])
    await write_audit(
        pool,
        action="mailbox.password.reveal",
        actor_kind="admin",
        actor_id=admin.username,
        target_kind="mailbox",
        target_id=address,
    )
    return {"address": address, "password": plaintext}


# ─── Providers ─────────────────────────────────────────────────────────────
@router.get("/providers", response_class=HTMLResponse)
async def providers_list(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
):
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        """
        SELECT p.*,
          (SELECT COUNT(*) FROM mailboxes m WHERE m.provider_id = p.id) AS mailbox_count
        FROM providers p ORDER BY p.name
        """
    )
    return templates.TemplateResponse(
        request, "providers.html", {"admin": admin, "providers": rows}
    )


# ─── API keys ──────────────────────────────────────────────────────────────
@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_list(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
):
    pool = request.app.state.db_pool
    where = "TRUE"
    args: list[Any] = []
    if not admin.is_super_admin:
        args.append(admin.tenant_id)
        where = f"tenant_id = ${len(args)}"
    rows = await pool.fetch(
        f"""
        SELECT k.id, k.name, k.key_prefix, k.is_admin_scope, k.is_active,
               k.created_at, k.last_used_at, k.revoked_at,
               t.slug AS tenant_slug
        FROM api_keys k LEFT JOIN tenants t ON k.tenant_id = t.id
        WHERE {where}
        ORDER BY k.created_at DESC
        """,
        *args,
    )
    return templates.TemplateResponse(
        request, "api_keys.html", {"admin": admin, "keys": rows, "new_token": None}
    )


@router.post("/api-keys/new")
async def api_key_create(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
    name: Annotated[str, Form()],
    is_admin_scope: Annotated[str, Form()] = "",
):
    pool = request.app.state.db_pool
    tenant_id = admin.tenant_id
    if tenant_id is None:
        tenant_id = await pool.fetchval("SELECT id FROM tenants WHERE slug = 'default'")
    token, prefix, key_hash = generate_api_key()
    await pool.execute(
        """
        INSERT INTO api_keys (name, tenant_id, key_hash, key_prefix, is_admin_scope, is_active)
        VALUES ($1, $2, $3, $4, $5, TRUE)
        """,
        name, tenant_id, key_hash, prefix,
        is_admin_scope in ("on", "yes", "true", "1"),
    )
    await write_audit(
        pool, action="api_key.create", actor_kind="admin", actor_id=admin.username,
        target_kind="api_key", target_id=name,
    )

    where = "TRUE"
    args: list[Any] = []
    if not admin.is_super_admin:
        args.append(admin.tenant_id)
        where = f"tenant_id = ${len(args)}"
    rows = await pool.fetch(
        f"""
        SELECT k.id, k.name, k.key_prefix, k.is_admin_scope, k.is_active,
               k.created_at, k.last_used_at, k.revoked_at,
               t.slug AS tenant_slug
        FROM api_keys k LEFT JOIN tenants t ON k.tenant_id = t.id
        WHERE {where} ORDER BY k.created_at DESC
        """,
        *args,
    )
    return templates.TemplateResponse(
        request, "api_keys.html", {"admin": admin, "keys": rows, "new_token": token}
    )


@router.post("/api-keys/{key_id}/revoke")
async def api_key_revoke(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
    key_id: int,
):
    pool = request.app.state.db_pool
    extra = "" if admin.is_super_admin else " AND tenant_id = $2"
    args: list[Any] = [key_id] if admin.is_super_admin else [key_id, admin.tenant_id]
    await pool.execute(
        f"UPDATE api_keys SET is_active = FALSE, revoked_at = NOW() WHERE id = $1{extra}",
        *args,
    )
    await write_audit(
        pool, action="api_key.revoke", actor_kind="admin", actor_id=admin.username,
        target_kind="api_key", target_id=str(key_id),
    )
    return RedirectResponse(url="/admin/api-keys", status_code=303)


# ─── Tenants (super-admin only) ───────────────────────────────────────────
@router.get("/tenants", response_class=HTMLResponse)
async def tenants_list(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_super_admin)],
):
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        """
        SELECT t.*,
          (SELECT COUNT(*) FROM mailboxes m WHERE m.tenant_id = t.id) AS mailbox_count,
          (SELECT COUNT(*) FROM codes c WHERE c.tenant_id = t.id) AS code_count,
          (SELECT COUNT(*) FROM api_keys k WHERE k.tenant_id = t.id) AS key_count
        FROM tenants t ORDER BY t.id
        """
    )
    return templates.TemplateResponse(
        request, "tenants.html", {"admin": admin, "tenants": rows}
    )


@router.post("/tenants/new")
async def tenant_create(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_super_admin)],
    slug: Annotated[str, Form()],
    name: Annotated[str, Form()],
):
    pool = request.app.state.db_pool
    await pool.execute(
        "INSERT INTO tenants (slug, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        slug, name,
    )
    return RedirectResponse(url="/admin/tenants", status_code=303)


# ─── Admins (super-admin only) ────────────────────────────────────────────
@router.get("/admins", response_class=HTMLResponse)
async def admins_list(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_super_admin)],
):
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        """
        SELECT a.id, a.username, a.is_super_admin, a.is_active,
               a.created_at, a.last_login_at,
               t.slug AS tenant_slug
        FROM admins a LEFT JOIN tenants t ON a.tenant_id = t.id
        ORDER BY a.is_super_admin DESC, a.username
        """
    )
    tenants = await pool.fetch("SELECT id, slug FROM tenants ORDER BY slug")
    return templates.TemplateResponse(
        request, "admins.html", {"admin": admin, "admins": rows, "tenants": tenants}
    )


@router.post("/admins/new")
async def admin_create(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_super_admin)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    tenant_id: Annotated[str, Form()] = "",
    is_super: Annotated[str, Form()] = "",
):
    pool = request.app.state.db_pool
    is_super_bool = is_super in ("on", "yes", "true", "1")
    tid = None if is_super_bool else (int(tenant_id) if tenant_id else None)
    if not is_super_bool and tid is None:
        raise HTTPException(status_code=400, detail="Tenant required for non-super admin")
    await pool.execute(
        """
        INSERT INTO admins (username, password_hash, is_super_admin, tenant_id, is_active)
        VALUES ($1, $2, $3, $4, TRUE)
        """,
        username, hash_password(password), is_super_bool, tid,
    )
    return RedirectResponse(url="/admin/admins", status_code=303)


@router.post("/admins/{admin_id}/deactivate")
async def admin_deactivate(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_super_admin)],
    admin_id: int,
):
    if admin_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    pool = request.app.state.db_pool
    await pool.execute("UPDATE admins SET is_active = FALSE WHERE id = $1", admin_id)
    return RedirectResponse(url="/admin/admins", status_code=303)


# ─── Extractor patterns + playground ──────────────────────────────────────
@router.get("/api/oauth-pending-count")
async def oauth_pending_count(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
):
    """Tiny JSON used by the sidebar badge."""
    pool = request.app.state.db_pool
    where = ["status IN ('pending_oauth_manual', 'pending_oauth_headless')", "is_active = TRUE"]
    args: list[Any] = []
    if not admin.is_super_admin:
        args.append(admin.tenant_id)
        where.append(f"tenant_id = ${len(args)}")
    count = await pool.fetchval(
        f"SELECT COUNT(*) FROM mailboxes WHERE {' AND '.join(where)}", *args
    )
    return {"count": int(count or 0)}


@router.get("/oauth-pending", response_class=HTMLResponse)
async def oauth_pending(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
):
    """List of mailboxes awaiting human OAuth consent.
    Headless attempts failed (challenge / captcha / MFA), so the operator
    needs to click the consent_url in their own browser one time."""
    pool = request.app.state.db_pool
    where = ["m.status IN ('pending_oauth_manual', 'pending_oauth_headless')", "m.is_active = TRUE"]
    args: list[Any] = []
    if not admin.is_super_admin:
        args.append(admin.tenant_id)
        where.append(f"m.tenant_id = ${len(args)}")
    where_sql = " AND ".join(where)
    rows = await pool.fetch(
        f"""
        SELECT m.address, m.status, m.purpose, m.created_at,
               m.oauth_consent_url, m.oauth_consent_expires_at,
               m.oauth_last_error, m.headless_attempt_count,
               m.headless_last_attempt_at,
               p.name AS provider_name,
               t.slug AS tenant_slug
        FROM mailboxes m
        LEFT JOIN providers p ON m.provider_id = p.id
        LEFT JOIN tenants t ON m.tenant_id = t.id
        WHERE {where_sql}
        ORDER BY m.created_at ASC
        """,
        *args,
    )
    return templates.TemplateResponse(
        request, "oauth_pending.html", {"admin": admin, "mailboxes": rows}
    )


@router.get("/silent", response_class=HTMLResponse)
async def silent_mailboxes(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
):
    """Mailboxes in rely_on_groups with no codes received recently — likely
    misconfigured forwarding or banned source account."""
    pool = request.app.state.db_pool
    where = ["m.status = 'rely_on_groups'", "m.is_active = TRUE"]
    args: list[Any] = []
    if not admin.is_super_admin:
        args.append(admin.tenant_id)
        where.append(f"m.tenant_id = ${len(args)}")
    where_sql = " AND ".join(where)
    rows = await pool.fetch(
        f"""
        SELECT m.address, m.purpose, m.created_at, m.last_code_at,
               m.last_forwarding_probe_at, m.forwarding_probe_status,
               m.forwarding_probe_error, m.forwarding_target,
               p.name AS provider_name,
               (NOW() - COALESCE(m.last_code_at, m.created_at)) AS quiet_for
        FROM mailboxes m
        LEFT JOIN providers p ON m.provider_id = p.id
        WHERE {where_sql}
        ORDER BY COALESCE(m.last_code_at, m.created_at) ASC
        LIMIT 200
        """,
        *args,
    )
    return templates.TemplateResponse(
        request, "silent.html", {"admin": admin, "mailboxes": rows}
    )


@router.get("/extractors", response_class=HTMLResponse)
async def extractors_list(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
):
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        "SELECT * FROM extractor_patterns ORDER BY priority, platform, name"
    )
    return templates.TemplateResponse(
        request, "extractors.html", {"admin": admin, "extractors": rows}
    )


@router.get("/extractors/playground", response_class=HTMLResponse)
async def extractor_playground_get(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
):
    return templates.TemplateResponse(
        request,
        "extractor_playground.html",
        {"admin": admin, "result": None, "input": None},
    )


@router.post("/extractors/playground")
async def extractor_playground_post(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
    sender: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    body: Annotated[str, Form()] = "",
):
    from workers.extractor import run_extraction
    pool = request.app.state.db_pool
    patterns = await pool.fetch(
        "SELECT * FROM extractor_patterns WHERE is_active = TRUE ORDER BY priority"
    )
    result = run_extraction(
        sender=sender, subject=subject, body=body, patterns=patterns
    )
    return templates.TemplateResponse(
        request,
        "extractor_playground.html",
        {
            "admin": admin,
            "result": result,
            "input": {"sender": sender, "subject": subject, "body": body},
        },
    )


# ─── Metrics (simple aggregations + Plotly) ───────────────────────────────
@router.get("/metrics", response_class=HTMLResponse)
async def metrics(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
):
    pool = request.app.state.db_pool
    where = ""
    args: list[Any] = []
    if not admin.is_super_admin:
        args.append(admin.tenant_id)
        where = " AND tenant_id = $1"

    # Codes per hour, last 24h
    per_hour = await pool.fetch(
        f"""
        SELECT date_trunc('hour', received_at) AS hour, COUNT(*) AS n
        FROM codes
        WHERE received_at > NOW() - INTERVAL '24 hours'{where}
        GROUP BY hour ORDER BY hour
        """,
        *args,
    )

    # Codes by platform
    by_platform = await pool.fetch(
        f"""
        SELECT COALESCE(platform, 'unknown') AS platform, COUNT(*) AS n
        FROM codes
        WHERE received_at > NOW() - INTERVAL '7 days'{where}
        GROUP BY platform ORDER BY n DESC
        """,
        *args,
    )

    return templates.TemplateResponse(
        request,
        "metrics.html",
        {
            "admin": admin,
            "per_hour": [
                {"hour": r["hour"].isoformat(), "n": r["n"]} for r in per_hour
            ],
            "by_platform": [{"platform": r["platform"], "n": r["n"]} for r in by_platform],
            "per_hour_json": json.dumps([
                {"hour": r["hour"].isoformat(), "n": r["n"]} for r in per_hour
            ]),
            "by_platform_json": json.dumps([
                {"platform": r["platform"], "n": r["n"]} for r in by_platform
            ]),
        },
    )
