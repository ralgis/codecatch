"""OAuth callback / paste-back endpoints.

Two ways the user can finish a manual OAuth flow:

1. **Native callback** (used when redirect_uri actually points at codecatch):
   provider redirects browser to /oauth/callback?code=...&state=... ; we
   handle it automatically.

2. **Paste-back** (used when redirect_uri is the Microsoft nativeclient
   URL — which is the only redirect URI Mozilla's Thunderbird client_id
   has registered). The user copies the final URL from their address bar
   and pastes it into a form at /admin/oauth-paste — codecatch extracts
   `code` and `state` and runs the same exchange.

Security comes from the `state` parameter — only known to us and to the
user who started the flow.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Annotated

import httpx
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from codecatch.crypto import decrypt, encrypt
from codecatch.logging_setup import get_logger

router = APIRouter()
log = get_logger("oauth_callback")

REDIRECT_URI_GOOGLE = "http://127.0.0.1:8765/callback"   # same constants as worker
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
    return await _do_exchange(request, code=code, state=state)


@router.post("/admin/oauth-paste")
async def oauth_paste(
    request: Request,
    pasted_url: Annotated[str, Form()],
):
    """Operator finishes OAuth manually, then pastes the full address-bar URL here.
    We extract `code` and `state` and reuse the same /oauth/callback logic.

    Accepts:
      - full URL (https://login.microsoftonline.com/common/oauth2/nativeclient?code=...&state=...)
      - just the query string (code=...&state=...)
      - or any URL containing those params anywhere
    """
    pasted = pasted_url.strip()
    if not pasted:
        return _html_error("Empty paste — copy the URL from your address bar and paste here.")

    parsed = urllib.parse.urlparse(pasted)
    qs = parsed.query if parsed.query else pasted   # accept raw query string too
    params = urllib.parse.parse_qs(qs)
    code_list = params.get("code", [])
    state_list = params.get("state", [])
    error_list = params.get("error", [])

    if error_list:
        return _html_error(f"Provider returned error in URL: {error_list[0]}")
    if not code_list or not state_list:
        return _html_error(
            "Could not find `code` and `state` in the pasted URL. "
            "Make sure you copied the FULL address-bar URL after Microsoft redirected you."
        )

    # Delegate to the same logic as the GET callback
    return await _do_exchange(
        request, code=code_list[0], state=state_list[0],
    )


async def _do_exchange(request: Request, *, code: str, state: str) -> HTMLResponse:
    """Shared token-exchange logic used by both /oauth/callback and /admin/oauth-paste."""
    pool = request.app.state.db_pool
    flow_row = await pool.fetchrow(
        "SELECT value FROM settings WHERE key = $1", f"oauth.flow.{state}"
    )
    if not flow_row:
        return _html_error("Unknown or expired OAuth state — start over from /admin/oauth-pending.")

    flow = flow_row["value"] if isinstance(flow_row["value"], dict) else json.loads(flow_row["value"])
    address = flow["address"]
    provider_kind = flow["provider_kind"]
    client_id = flow["client_id"]
    scopes = flow["scopes"]

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
        return _html_error("Provider returned no refresh_token (consent may have been incomplete)")

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
