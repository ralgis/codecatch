"""Playwright-driven setup of email forwarding in consumer webmail providers.

Right now we cover **outlook.live.com** (Hotmail/Outlook personal accounts).
Gmail consumer would need a different selector set + their forwarding flow
also requires a verification code clicked at the receiving end — more work.

Triggered explicitly by `POST /api/v1/mailboxes/{address}/setup-forwarding`
or by the admin UI button. Not part of the always-running worker loop —
each invocation creates a temporary Playwright context, drives the flow,
records result on the mailbox row, and exits.

Outcome statuses written to mailboxes:
    forwarding_probe_status   ='ok'     (we believe forwarding is set)
                               'failed' (something went wrong)
    forwarding_probe_error    explanatory text
    forwarding_target         set to the target group address on success
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import os

import asyncpg

from codecatch.config import get_settings
from codecatch.crypto import decrypt
from codecatch.logging_setup import get_logger
from workers.oauth_worker import (
    ANTI_AUTOMATION_ARGS,
    PROFILES_DIR,
    REALISTIC_LOCALE,
    REALISTIC_TIMEZONE,
    REALISTIC_UA,
    REALISTIC_VIEWPORT,
)

log = get_logger("forwarding_setup")


@dataclass
class SetupResult:
    ok: bool
    detail: str


async def configure_outlook_forwarding(
    pool: asyncpg.Pool,
    *,
    hotmail_address: str,
    hotmail_password: str,
    forward_to: str,
    keep_copy: bool = True,
    proxy_url: str | None = None,
) -> SetupResult:
    """Open Playwright, log into outlook.live.com, enable forwarding rule.

    Best-effort: if MS shows MFA / captcha / unfamiliar location prompt, we
    bail and the caller may need to do this account by hand once.
    """
    s = get_settings()

    # Lazy import — Playwright is only present in the workers container,
    # not in api. Caller must run this in an environment that has it.
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return SetupResult(
                False,
                "Playwright/patchright not available in this container. Trigger via workers.",
            )

    safe_addr = hotmail_address.replace("@", "_at_").replace("/", "_")
    profile_dir = os.path.join(PROFILES_DIR, safe_addr)
    os.makedirs(profile_dir, exist_ok=True)

    async with async_playwright() as pw:
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
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}
        context = await pw.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            return await _drive(page, hotmail_address, hotmail_password, forward_to, keep_copy)
        finally:
            await context.close()


async def _drive(page, addr: str, password: str, forward_to: str, keep_copy: bool) -> SetupResult:
    s = get_settings()
    timeout = s.playwright_timeout_sec * 1000

    # 1. Land on outlook.live.com — should redirect to login
    try:
        await page.goto("https://outlook.live.com/mail/0/options/mail/forwarding", timeout=timeout)
    except Exception:
        return SetupResult(False, "Timed out navigating to outlook.live.com")

    # 2. Login flow (same as OAuth worker's MS flow)
    try:
        await page.wait_for_selector('input[type="email"], input[name="loginfmt"]', timeout=20000)
        await page.fill('input[type="email"], input[name="loginfmt"]', addr)
        await page.click('input[type="submit"], #idSIButton9')

        await page.wait_for_selector('input[type="password"], input[name="passwd"]', timeout=20000)
        await asyncio.sleep(1)
        await page.fill('input[type="password"], input[name="passwd"]', password)
        await page.click('input[type="submit"], #idSIButton9')

        # "Stay signed in?" dialog
        try:
            await page.wait_for_selector('#idBtn_Back, #idSIButton9', timeout=10000)
            await page.click('#idBtn_Back, input[value="No"]')
        except Exception:
            pass
    except Exception as e:
        return SetupResult(False, f"Login flow stuck: {str(e)[:200]}")

    # 3. Wait for forwarding page to load (sometimes goes via /mail/0/ first)
    try:
        await page.wait_for_url("**outlook.live.com/mail/**/options/mail/forwarding**", timeout=30000)
    except Exception:
        # navigate explicitly
        try:
            await page.goto("https://outlook.live.com/mail/0/options/mail/forwarding", timeout=20000)
        except Exception:
            return SetupResult(False, "Could not reach the forwarding settings page")

    # 4. Detect challenges
    page_text = (await page.content()).lower()
    for marker in ("verify your identity", "is this you", "unusual sign-in",
                   "we need to verify"):
        if marker in page_text:
            return SetupResult(False, f"Microsoft challenged for: '{marker}'. Manual intervention required.")

    # 5. Find the forwarding toggle. Outlook's exact selectors change often;
    # this is best-effort — we look for any role=switch labelled with 'forward'.
    try:
        toggle = page.locator("[role='switch']").first
        await toggle.wait_for(timeout=15000)
        is_on = await toggle.get_attribute("aria-checked")
        if is_on != "true":
            await toggle.click()
            await asyncio.sleep(1.5)
    except Exception:
        return SetupResult(False, "Could not find the forwarding toggle on the settings page (selectors may have changed)")

    # 6. Fill the forwarding address
    try:
        addr_input = page.locator("input[aria-label*='forward'], input[placeholder*='@']").first
        await addr_input.wait_for(timeout=10000)
        await addr_input.fill(forward_to)
    except Exception:
        return SetupResult(False, "Could not find the 'forward to' input field")

    # 7. Keep-copy checkbox (best effort)
    if keep_copy:
        try:
            keep_box = page.locator("input[type='checkbox']").first
            checked = await keep_box.is_checked()
            if not checked:
                await keep_box.check()
        except Exception:  # noqa: BLE001
            pass  # keep-copy is non-fatal

    # 8. Save
    try:
        save_btn = page.locator("button:has-text('Save'), button[aria-label='Save']").first
        await save_btn.click(timeout=10000)
        await asyncio.sleep(2)
    except Exception:
        return SetupResult(False, "Could not find the Save button")

    return SetupResult(True, f"Forwarding to {forward_to} configured (keep_copy={keep_copy})")


async def configure_for_mailbox(pool: asyncpg.Pool, address: str, forward_to: str | None = None) -> SetupResult:
    """High-level: look up mailbox, decrypt password, run Playwright flow,
    record outcome on the mailbox row."""
    mb = await pool.fetchrow(
        """
        SELECT m.address, m.headless_proxy_url, m.tenant_id, mp.password_encrypted,
               p.auth_kind
        FROM mailboxes m
        JOIN providers p ON m.provider_id = p.id
        LEFT JOIN mailbox_passwords mp
            ON mp.mailbox_address = m.address AND mp.is_current = TRUE
        WHERE m.address = $1
        """,
        address,
    )
    if not mb:
        return SetupResult(False, "Mailbox not found")
    if not mb["password_encrypted"]:
        return SetupResult(False, "No stored password — cannot drive Playwright login")
    if mb["auth_kind"] != "oauth_microsoft":
        return SetupResult(False, "This automation supports only Microsoft consumer mailboxes (hotmail/outlook.com)")

    if forward_to is None:
        group = await pool.fetchrow(
            """
            SELECT address FROM mailboxes
            WHERE tenant_id = $1 AND is_group = TRUE AND is_active = TRUE
              AND status IN ('direct_active', 'oauth_active')
            ORDER BY created_at LIMIT 1
            """,
            mb["tenant_id"],
        )
        if not group:
            return SetupResult(False, "No active group inbox to forward to")
        forward_to = group["address"]

    try:
        password = decrypt(mb["password_encrypted"])
    except ValueError:
        return SetupResult(False, "Password decryption failed")

    result = await configure_outlook_forwarding(
        pool,
        hotmail_address=address,
        hotmail_password=password,
        forward_to=forward_to,
        keep_copy=True,
        proxy_url=mb["headless_proxy_url"],
    )

    await pool.execute(
        """
        UPDATE mailboxes SET
            forwarding_target = COALESCE($2, forwarding_target),
            forwarding_probe_status = $3,
            forwarding_probe_error = $4,
            last_forwarding_probe_at = NOW()
        WHERE address = $1
        """,
        address,
        forward_to if result.ok else None,
        "ok" if result.ok else "failed",
        result.detail,
    )
    log.info(
        "forwarding_setup.done", address=address, target=forward_to,
        ok=result.ok, detail=result.detail[:150],
    )
    return result
