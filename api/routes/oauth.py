"""Public OAuth callback endpoint.

Used by the manual-fallback flow: when headless can't complete (MFA / captcha),
codecatch stores a consent_url and the human opens it in a browser. After the
human clicks Accept, the provider redirects to this endpoint with ?code=...
We then exchange the code for refresh_token and flip the mailbox to oauth_active.

Note: this endpoint is unauthenticated — security comes from the `state`
parameter which is only known to us and to the user who started the flow.
"""
from __future__ import annotations

import json
from typing import Annotated

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from codecatch.crypto import decrypt, encrypt
from codecatch.logging_setup import get_logger

router = APIRouter()
log = get_logger("oauth_callback")

REDIRECT_URI_GOOGLE = "http://127.0.0.1:8765/callback"   # same constant as worker
REDIRECT_URI_MS = "https://login.microsoftonline.com/common/oauth2/nativeclient"


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    request: Request,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
):
    if error:
        return _html_error(f"Provider returned error: {error}")
    if not code or not state:
        return _html_error("Missing code or state parameter")

    pool = request.app.state.db_pool
    flow_row = await pool.fetchrow(
        "SELECT value FROM settings WHERE key = $1", f"oauth.flow.{state}"
    )
    if not flow_row:
        return _html_error("Unknown or expired OAuth state — start over from /admin/mailboxes")

    flow = flow_row["value"] if isinstance(flow_row["value"], dict) else json.loads(flow_row["value"])
    address = flow["address"]
    provider_kind = flow["provider_kind"]
    client_id = flow["client_id"]
    scopes = flow["scopes"]

    # Load provider for client_secret (Google only)
    prov_row = await pool.fetchrow(
        """
        SELECT auth_kind, oauth_strategy, oauth_client_secret_encrypted
        FROM providers
        WHERE oauth_client_id = $1 AND auth_kind = $2
        """,
        client_id, provider_kind,
    )
    client_secret: str | None = None
    if prov_row:
        if prov_row["oauth_client_secret_encrypted"]:
            try:
                client_secret = decrypt(prov_row["oauth_client_secret_encrypted"])
            except ValueError:
                client_secret = None
        elif prov_row["oauth_strategy"] == "thunderbird" and provider_kind == "oauth_google":
            from workers.oauth_worker import THUNDERBIRD_GOOGLE_SECRET
            client_secret = THUNDERBIRD_GOOGLE_SECRET

    if provider_kind == "oauth_google":
        token_url = "https://oauth2.googleapis.com/token"
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI_GOOGLE,
        }
    else:
        token_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
        data = {
            "client_id": client_id,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI_MS,
            "scope": " ".join(scopes),
        }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(token_url, data={k: v for k, v in data.items() if v is not None})
            r.raise_for_status()
            tokens = r.json()
    except Exception as e:  # noqa: BLE001
        log.exception("oauth_callback.token_exchange_failed", address=address, error=str(e))
        return _html_error(f"Failed to exchange code for token: {e}")

    refresh = tokens.get("refresh_token")
    if not refresh:
        return _html_error("Provider returned no refresh_token (this often means consent was incomplete)")

    await pool.execute(
        """
        UPDATE mailboxes SET
            status = 'oauth_active',
            oauth_refresh_token_encrypted = $2,
            oauth_consented_at = NOW(),
            oauth_consent_url = NULL,
            oauth_consent_expires_at = NULL,
            oauth_last_error = NULL,
            updated_at = NOW()
        WHERE address = $1
        """,
        address, encrypt(refresh),
    )
    # Remove the one-time state record
    await pool.execute("DELETE FROM settings WHERE key = $1", f"oauth.flow.{state}")

    log.info("oauth_callback.success", address=address)
    return HTMLResponse(_html_success(address))


def _html_error(msg: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html><html><head><title>OAuth error</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
        </head><body class="container py-5">
        <div class="alert alert-danger">
            <h4>OAuth flow failed</h4>
            <p>{msg}</p>
            <a href="/admin/mailboxes" class="btn btn-secondary">Back to mailboxes</a>
        </div></body></html>""",
        status_code=400,
    )


def _html_success(address: str) -> str:
    return f"""<!DOCTYPE html><html><head><title>OAuth OK</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head><body class="container py-5">
    <div class="alert alert-success">
        <h4>Mailbox connected</h4>
        <p>OAuth flow completed for <code>{address}</code>. You can close this tab — codecatch is now reading mail for this address.</p>
        <a href="/admin/mailboxes" class="btn btn-primary">Back to mailboxes</a>
    </div></body></html>"""
