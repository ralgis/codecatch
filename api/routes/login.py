"""Login/logout for the admin UI (session-cookie based)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from api.templating import templates
from codecatch.audit import write_audit
from codecatch.auth import (
    SESSION_COOKIE,
    CurrentAdmin,
    get_current_admin_optional,
    sign_session,
)
from codecatch.crypto import verify_password

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    admin: Annotated[CurrentAdmin | None, Depends(get_current_admin_optional)],
    error: str | None = None,
):
    if admin:
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": error}
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    pool = request.app.state.db_pool
    row = await pool.fetchrow(
        """
        SELECT id, username, password_hash, is_active
        FROM admins WHERE username = $1
        """,
        username,
    )
    ok = bool(row) and row["is_active"] and verify_password(password, row["password_hash"])
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", "")[:255]

    if not ok:
        await write_audit(
            pool,
            action="admin.login",
            actor_kind="admin",
            actor_id=username,
            ip_address=client_ip,
            user_agent=user_agent,
            success=False,
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password"},
            status_code=401,
        )

    await pool.execute("UPDATE admins SET last_login_at = NOW() WHERE id = $1", row["id"])
    await write_audit(
        pool,
        action="admin.login",
        actor_kind="admin",
        actor_id=username,
        ip_address=client_ip,
        user_agent=user_agent,
        success=True,
    )
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sign_session(row["id"]),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,  # 30 days
        secure=False,  # set True in production behind https
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
