"""Periodic OAuth refresh job.

For every mailbox in status='oauth_active' whose cached access_token is
either missing or expiring within 10 minutes, hit the provider's /token
endpoint with grant_type=refresh_token, store the new access_token (and
the rotated refresh_token if the provider returned one).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx

from codecatch.crypto import decrypt, encrypt
from codecatch.logging_setup import get_logger

log = get_logger("oauth_refresh")

REFRESH_MIN_LIFETIME = timedelta(minutes=10)
REFRESH_TICK_INTERVAL = 60  # seconds between scans


class OAuthRefresher:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        log.info("oauth_refresh.start")
        while not self._shutdown.is_set():
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                log.exception("oauth_refresh.tick_failed", error=str(e))
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=REFRESH_TICK_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._shutdown.set()

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        deadline = now + REFRESH_MIN_LIFETIME
        rows = await self.pool.fetch(
            """
            SELECT m.address, m.oauth_refresh_token_encrypted,
                   m.oauth_access_token_expires_at,
                   p.auth_kind, p.oauth_client_id, p.oauth_client_secret_encrypted,
                   p.oauth_strategy, p.oauth_scopes
            FROM mailboxes m
            JOIN providers p ON m.provider_id = p.id
            WHERE m.status = 'oauth_active'
              AND m.is_active = TRUE
              AND m.oauth_refresh_token_encrypted IS NOT NULL
              AND (
                   m.oauth_access_token_encrypted IS NULL
                OR m.oauth_access_token_expires_at IS NULL
                OR m.oauth_access_token_expires_at < $1
              )
            LIMIT 100
            """,
            deadline,
        )
        for r in rows:
            await self._refresh_one(r)

    async def _refresh_one(self, row: asyncpg.Record) -> None:
        try:
            refresh_token = decrypt(row["oauth_refresh_token_encrypted"])
        except ValueError:
            log.error("oauth_refresh.decrypt_failed", address=row["address"])
            return

        client_secret: str | None = None
        if row["oauth_client_secret_encrypted"]:
            try:
                client_secret = decrypt(row["oauth_client_secret_encrypted"])
            except ValueError:
                pass
        if not client_secret and row["oauth_strategy"] == "thunderbird" and row["auth_kind"] == "oauth_google":
            from workers.oauth_worker import THUNDERBIRD_GOOGLE_SECRET
            client_secret = THUNDERBIRD_GOOGLE_SECRET

        if row["auth_kind"] == "oauth_google":
            token_url = "https://oauth2.googleapis.com/token"
            data = {
                "client_id": row["oauth_client_id"],
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        else:  # microsoft
            token_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
            data = {
                "client_id": row["oauth_client_id"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": " ".join(row["oauth_scopes"] or []),
            }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(token_url, data={k: v for k, v in data.items() if v is not None})
                if resp.status_code != 200:
                    log.warning(
                        "oauth_refresh.failed",
                        address=row["address"],
                        status=resp.status_code,
                        body=resp.text[:200],
                    )
                    await self.pool.execute(
                        "UPDATE mailboxes SET oauth_last_error = $2, updated_at = NOW() WHERE address = $1",
                        row["address"], f"refresh failed: {resp.status_code} {resp.text[:200]}",
                    )
                    return
                tokens = resp.json()
        except Exception as e:  # noqa: BLE001
            log.exception("oauth_refresh.request_error", address=row["address"], error=str(e))
            return

        access_token = tokens.get("access_token")
        expires_in = int(tokens.get("expires_in", 3600))
        new_refresh = tokens.get("refresh_token")  # MS rotates, Google sometimes does

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        await self.pool.execute(
            """
            UPDATE mailboxes SET
                oauth_access_token_encrypted = $2,
                oauth_access_token_expires_at = $3,
                oauth_refresh_token_encrypted = COALESCE($4, oauth_refresh_token_encrypted),
                oauth_last_error = NULL,
                last_status_check_at = NOW(),
                updated_at = NOW()
            WHERE address = $1
            """,
            row["address"], encrypt(access_token), expires_at,
            encrypt(new_refresh) if new_refresh else None,
        )
        log.info(
            "oauth_refresh.ok",
            address=row["address"],
            expires_in_sec=expires_in,
            rotated=bool(new_refresh),
        )
