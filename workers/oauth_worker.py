"""OAuth provisioning worker — Playwright-driven headless consent.

For each mailbox with status='pending_oauth_headless':
  1. Build OAuth authorize URL using the provider's client_id (Mozilla
     Thunderbird's by default).
  2. Launch Playwright Chromium, navigate to authorize URL.
  3. Fill email, fill password, click through consent.
  4. Catch redirect to http://localhost?code=... and capture the code.
  5. Exchange the code for refresh_token via the provider's /token endpoint.
  6. Store refresh_token (encrypted) and flip status to 'oauth_active'.

If any step encounters an unexpected page (MFA challenge, captcha, "Was
this you?"), we save the authorize URL into mailbox.oauth_consent_url and
flip status to 'pending_oauth_manual' so a human can finish in a normal
browser. The callback endpoint (/oauth/callback) takes care of the human
case identically — just without us doing the clicking.

Tested for happy path on fresh accounts. Production-grade challenge
handling is a v1.1 concern.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass

import os

import asyncpg
import httpx
# patchright is a drop-in replacement for playwright with anti-detection patches.
# Falls back to vanilla playwright if patchright is unavailable.
try:
    from patchright.async_api import (
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
except ImportError:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )

# Realistic browser fingerprint for OAuth consent. Pinned to a recent Chrome
# stable version so it doesn't stand out as "headless" — match this against
# patchright's bundled chromium major version periodically.
REALISTIC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
REALISTIC_VIEWPORT = {"width": 1920, "height": 1080}
REALISTIC_LOCALE = "en-US"
REALISTIC_TIMEZONE = "Europe/Berlin"
ANTI_AUTOMATION_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

PROFILES_DIR = "/app/playwright_profiles"

from codecatch.config import get_settings
from codecatch.crypto import decrypt, encrypt
from codecatch.logging_setup import get_logger

log = get_logger("oauth_worker")

REDIRECT_URI_MS = "https://login.microsoftonline.com/common/oauth2/nativeclient"
REDIRECT_URI_GOOGLE = "http://127.0.0.1:8765/callback"   # we intercept this in Playwright

# Mozilla Thunderbird's Microsoft client_id has ONLY the nativeclient URI
# registered. We tested http://localhost:8080/oauth/callback — Microsoft
# rejected it with "redirect_uri is not valid". So for the manual fallback
# the user lands on the nativeclient warning page; recovering the auth code
# from there requires either a DevTools trick or registering our own Azure
# app with a real callback URL. See /admin/oauth-pending UI for paste-back
# flow as a stop-gap.

# Public Thunderbird OAuth client_id/secret for Google. "secret" here is not
# really secret — it's a marker required by Google's installed-app flow. It
# leaked years ago as part of TB's open-source code and Google considers
# installed-app secrets non-confidential by design.
THUNDERBIRD_GOOGLE_SECRET = "kSmqreRr0qwBWJgbf5Y-PjSU"


@dataclass
class OAuthAttempt:
    address: str
    provider_kind: str            # 'oauth_google' | 'oauth_microsoft'
    client_id: str
    client_secret: str | None
    scopes: list[str]
    password: str
    proxy_url: str | None


class OAuthWorker:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._shutdown = asyncio.Event()
        self._browser: Browser | None = None
        self._pw_ctx = None

    async def run(self) -> None:
        log.info("oauth_worker.start")
        async with async_playwright() as pw:
            self._pw_ctx = pw
            try:
                while not self._shutdown.is_set():
                    try:
                        await self._tick()
                    except Exception as e:  # noqa: BLE001
                        log.exception("oauth_worker.tick_failed", error=str(e))
                    try:
                        await asyncio.wait_for(self._shutdown.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        pass
            finally:
                if self._browser:
                    await self._browser.close()

    async def stop(self) -> None:
        self._shutdown.set()

    async def _tick(self) -> None:
        attempt = await self._claim_next()
        if not attempt:
            return
        log.info("oauth_worker.claimed", address=attempt.address, provider=attempt.provider_kind)
        try:
            await self._attempt_headless(attempt)
        except Exception as e:  # noqa: BLE001
            log.exception("oauth_worker.headless_failed", address=attempt.address, error=str(e))
            await self._fall_through_to_manual(attempt, reason=str(e)[:300])

    async def _claim_next(self) -> OAuthAttempt | None:
        row = await self.pool.fetchrow(
            """
            UPDATE mailboxes m
            SET status = 'pending_oauth_headless',
                headless_attempt_count = headless_attempt_count + 1,
                headless_last_attempt_at = NOW(),
                updated_at = NOW()
            WHERE m.address = (
                SELECT m2.address FROM mailboxes m2
                WHERE m2.status = 'pending_oauth_headless'
                  AND m2.is_active = TRUE
                  AND m2.headless_attempt_count < 3
                ORDER BY m2.created_at ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING m.address, m.headless_proxy_url, m.provider_id
            """
        )
        if not row:
            return None

        prov = await self.pool.fetchrow(
            "SELECT * FROM providers WHERE id = $1", row["provider_id"]
        )
        pw_row = await self.pool.fetchrow(
            """SELECT password_encrypted FROM mailbox_passwords
               WHERE mailbox_address = $1 AND is_current = TRUE""",
            row["address"],
        )
        if not pw_row:
            await self._mark_failed(row["address"], "No stored password for OAuth flow")
            return None

        try:
            password = decrypt(pw_row["password_encrypted"])
        except ValueError:
            await self._mark_failed(row["address"], "Password decryption failed")
            return None

        client_secret = None
        if prov["oauth_strategy"] == "thunderbird" and prov["auth_kind"] == "oauth_google":
            client_secret = THUNDERBIRD_GOOGLE_SECRET
        elif prov["oauth_client_secret_encrypted"]:
            client_secret = decrypt(prov["oauth_client_secret_encrypted"])

        return OAuthAttempt(
            address=row["address"],
            provider_kind=prov["auth_kind"],
            client_id=prov["oauth_client_id"] or "",
            client_secret=client_secret,
            scopes=list(prov["oauth_scopes"] or []),
            password=password,
            proxy_url=row["headless_proxy_url"],
        )

    async def _attempt_headless(self, attempt: OAuthAttempt) -> None:
        state = secrets.token_urlsafe(16)
        authorize_url = self._build_authorize_url(attempt, state)
        log.info("oauth_worker.authorize_url", address=attempt.address, url=authorize_url[:200])

        s = get_settings()
        assert self._pw_ctx is not None

        # Persistent profile per mailbox — cookies + history survive between
        # attempts, so Microsoft / Google see "the same device that consented
        # last week" rather than a brand-new headless browser each time.
        safe_addr = attempt.address.replace("@", "_at_").replace("/", "_")
        profile_dir = os.path.join(PROFILES_DIR, safe_addr)
        os.makedirs(profile_dir, exist_ok=True)

        launch_kwargs: dict = {
            "user_data_dir": profile_dir,
            "headless": s.playwright_headless,
            "user_agent": REALISTIC_UA,
            "viewport": REALISTIC_VIEWPORT,
            "locale": REALISTIC_LOCALE,
            "timezone_id": REALISTIC_TIMEZONE,
            "color_scheme": "light",
            "args": ANTI_AUTOMATION_ARGS,
        }
        if attempt.proxy_url:
            launch_kwargs["proxy"] = {"server": attempt.proxy_url}

        context: BrowserContext = await self._pw_ctx.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        debug_saved = False
        try:
            await page.goto(authorize_url, timeout=s.playwright_timeout_sec * 1000)
            try:
                code = await self._drive_login_flow(page, attempt)
            except Exception:
                await self._capture_debug(page, attempt.address)
                debug_saved = True
                raise
            if code is None:
                if not debug_saved:
                    await self._capture_debug(page, attempt.address)
                raise RuntimeError("Login flow ended without capturing authorization code")
            log.info("oauth_worker.code_captured", address=attempt.address)
        finally:
            await context.close()

        tokens = await self._exchange_code_for_tokens(attempt, code)
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise RuntimeError("Token endpoint returned no refresh_token")

        await self.pool.execute(
            """
            UPDATE mailboxes SET
                status = 'oauth_active',
                oauth_refresh_token_encrypted = $2,
                oauth_consented_at = NOW(),
                oauth_consent_url = NULL,
                oauth_consent_expires_at = NULL,
                oauth_last_error = NULL,
                last_error = NULL,
                updated_at = NOW()
            WHERE address = $1
            """,
            attempt.address, encrypt(refresh),
        )
        log.info("oauth_worker.oauth_active", address=attempt.address)

    async def _capture_debug(self, page, address: str) -> None:
        """Save screenshot + page HTML + URL on failure for postmortem.
        Files land in /app/debug/<address>/<timestamp>.{png,html,txt} so
        you can `docker cp` them out or mount the dir as a volume.
        """
        import os
        import time
        try:
            ts = time.strftime("%Y%m%d-%H%M%S")
            safe_addr = address.replace("@", "_at_").replace("/", "_")
            d = f"/app/debug/{safe_addr}"
            os.makedirs(d, exist_ok=True)
            await page.screenshot(path=f"{d}/{ts}.png", full_page=True)
            try:
                html = await page.content()
            except Exception:
                html = "<failed to read page.content>"
            with open(f"{d}/{ts}.html", "w", encoding="utf-8") as f:
                f.write(html)
            with open(f"{d}/{ts}.txt", "w", encoding="utf-8") as f:
                f.write(f"url: {page.url}\ntitle: {await page.title()}\n")
            log.warning(
                "oauth_worker.debug_captured",
                address=address, dir=d, ts=ts, url=page.url,
            )
        except Exception as e:  # noqa: BLE001
            log.error("oauth_worker.debug_capture_failed", address=address, error=str(e))

    async def _drive_login_flow(self, page: Page, attempt: OAuthAttempt) -> str | None:
        """Provider-specific click-through. Returns auth code if captured."""
        if attempt.provider_kind == "oauth_google":
            return await self._drive_google(page, attempt)
        if attempt.provider_kind == "oauth_microsoft":
            return await self._drive_microsoft(page, attempt)
        raise RuntimeError(f"Unsupported provider kind: {attempt.provider_kind}")

    async def _drive_google(self, page: Page, attempt: OAuthAttempt) -> str | None:
        s = get_settings()
        timeout = s.playwright_timeout_sec * 1000

        # Email step
        await page.fill('input[type="email"]', attempt.address, timeout=timeout)
        await page.click('button:has-text("Next"), #identifierNext button', timeout=timeout)

        # Password step
        await page.wait_for_selector('input[type="password"]', timeout=timeout)
        await asyncio.sleep(1.5)  # Google sometimes animates the page
        await page.fill('input[type="password"]', attempt.password, timeout=timeout)
        await page.click('button:has-text("Next"), #passwordNext button', timeout=timeout)

        # Consent — Google shows "Continue" then "Allow"
        for label in ("Continue", "Allow", "Accept", "Разрешить"):
            try:
                await page.wait_for_selector(f'button:has-text("{label}")', timeout=10000)
                await page.click(f'button:has-text("{label}")')
            except PlaywrightTimeoutError:
                continue

        # Wait for redirect to our redirect_uri with ?code=...
        code = await self._wait_for_code_in_url(page, REDIRECT_URI_GOOGLE, timeout=timeout)
        return code

    async def _drive_microsoft(self, page: Page, attempt: OAuthAttempt) -> str | None:
        s = get_settings()
        timeout = s.playwright_timeout_sec * 1000

        # Microsoft sometimes interleaves a "Verify your email" challenge before
        # the normal sign-in flow (typically when the IP is suspicious). Detect
        # it and try to pass it by re-typing the same email.
        try:
            # The data-testid wraps a <div> — the actual input is below it.
            proof_input = page.locator(
                '#proof-confirmation-email-input, '
                '[data-testid="proof-confirmation-email-input"] input'
            )
            if await proof_input.count() > 0:
                log.info("oauth_worker.proof_challenge", address=attempt.address)
                await proof_input.first.fill(attempt.address, timeout=15000)
                await page.click(
                    '[data-testid="primaryButton"] button, '
                    '[data-testid="primaryButton"], '
                    'button[type="submit"]',
                    timeout=15000,
                )
                await asyncio.sleep(2)
        except PlaywrightTimeoutError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("oauth_worker.proof_handling_failed", error=str(e)[:200])

        # Email step (normal sign-in form)
        await page.fill(
            'input[type="email"], input[name="loginfmt"]',
            attempt.address,
            timeout=timeout,
        )
        await page.click('input[type="submit"], #idSIButton9', timeout=timeout)

        # Password step
        await page.wait_for_selector('input[type="password"], input[name="passwd"]', timeout=timeout)
        await asyncio.sleep(1.5)
        await page.fill('input[type="password"], input[name="passwd"]', attempt.password, timeout=timeout)
        await page.click('input[type="submit"], #idSIButton9', timeout=timeout)

        # "Stay signed in?" — click No (less suspicious)
        try:
            await page.wait_for_selector('input[type="submit"], #idBtn_Back', timeout=10000)
            # Either "No" or "Yes" button
            await page.click('#idBtn_Back, input[value="No"]')
        except PlaywrightTimeoutError:
            pass

        # Consent
        try:
            await page.wait_for_selector('input[type="submit"], #idSIButton9', timeout=15000)
            await page.click('#idSIButton9, input[value="Accept"], input[value="Yes"]')
        except PlaywrightTimeoutError:
            pass

        code = await self._wait_for_code_in_url(page, REDIRECT_URI_MS, timeout=timeout)
        return code

    async def _wait_for_code_in_url(self, page: Page, redirect_prefix: str, timeout: int) -> str | None:
        """Poll page.url for the OAuth redirect with ?code=..."""
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            url = page.url or ""
            if url.startswith(redirect_prefix) or "?code=" in url:
                qs = urllib.parse.urlparse(url).query
                params = urllib.parse.parse_qs(qs)
                code = params.get("code", [None])[0]
                if code:
                    return code
            await asyncio.sleep(0.5)
        return None

    def _build_authorize_url(self, attempt: OAuthAttempt, state: str) -> str:
        if attempt.provider_kind == "oauth_google":
            base = "https://accounts.google.com/o/oauth2/v2/auth"
            params = {
                "client_id": attempt.client_id,
                "redirect_uri": REDIRECT_URI_GOOGLE,
                "response_type": "code",
                "scope": " ".join(attempt.scopes),
                "access_type": "offline",
                "prompt": "consent",
                "login_hint": attempt.address,
                "state": state,
            }
        else:  # microsoft
            base = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
            params = {
                "client_id": attempt.client_id,
                "redirect_uri": REDIRECT_URI_MS,
                "response_type": "code",
                "scope": " ".join(attempt.scopes),
                "prompt": "select_account",
                "login_hint": attempt.address,
                "state": state,
            }
        return f"{base}?{urllib.parse.urlencode(params)}"

    async def _exchange_code_for_tokens(self, attempt: OAuthAttempt, code: str) -> dict:
        if attempt.provider_kind == "oauth_google":
            url = "https://oauth2.googleapis.com/token"
            data = {
                "client_id": attempt.client_id,
                "client_secret": attempt.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI_GOOGLE,
            }
        else:
            url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
            data = {
                "client_id": attempt.client_id,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI_MS,
                "scope": " ".join(attempt.scopes),
            }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, data={k: v for k, v in data.items() if v is not None})
            r.raise_for_status()
            return r.json()

    async def _fall_through_to_manual(self, attempt: OAuthAttempt, reason: str) -> None:
        state = secrets.token_urlsafe(16)
        consent_url = self._build_authorize_url(attempt, state)
        # store state→address mapping in settings so /oauth/callback can resolve it
        await self.pool.execute(
            """
            INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            f"oauth.flow.{state}",
            json.dumps({
                "address": attempt.address,
                "provider_kind": attempt.provider_kind,
                "client_id": attempt.client_id,
                "scopes": attempt.scopes,
            }),
        )
        await self.pool.execute(
            """
            UPDATE mailboxes SET
                status = 'pending_oauth_manual',
                oauth_consent_url = $2,
                oauth_consent_expires_at = NOW() + INTERVAL '30 minutes',
                oauth_last_error = $3,
                updated_at = NOW()
            WHERE address = $1
            """,
            attempt.address, consent_url, reason,
        )
        log.warning("oauth_worker.fell_to_manual", address=attempt.address, reason=reason[:200])

    async def _mark_failed(self, address: str, reason: str) -> None:
        await self.pool.execute(
            """
            UPDATE mailboxes SET
                status = 'no_path', oauth_last_error = $2, updated_at = NOW()
            WHERE address = $1
            """,
            address, reason,
        )
