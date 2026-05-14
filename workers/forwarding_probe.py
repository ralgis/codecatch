"""Active forwarding probe.

For every mailbox with mode=auto/group_only/both that is in rely_on_groups
status and hasn't been probed recently, send a test email **to** that
mailbox via SMTP (from the group's own SMTP creds), then wait briefly for
that test message to land in a group inbox via forwarding. If it arrives →
forwarding works (status='ok'). If timeout → 'failed'.

The probe email has a recognisable subject like
    [codecatch-probe-<uuid>] forwarding test
We don't extract anything from probe mail — when normalizer sees probes
they go through code_writer like any other mail; we just won't have a code
extracted (no regex matches probe-style subjects).

To match arrivals to probes we look for the unique uuid in either
codes.subject (if it accidentally matches an extractor) or in the raw
inbox of the group via separate scan. For simplicity we keep things
loose: if any new mail arrives at this target in the probe window, count
it as ok. False positives are tolerable.
"""
from __future__ import annotations

import asyncio
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import asyncpg

from codecatch.crypto import decrypt
from codecatch.logging_setup import get_logger

log = get_logger("forwarding_probe")

PROBE_INTERVAL = timedelta(days=3)        # how often to re-probe each mailbox
PROBE_WAIT_SEC = 180                       # how long to wait for arrival
TICK_INTERVAL_SEC = 300                    # how often the scheduler runs


class ForwardingProbeWorker:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        log.info("forwarding_probe.start")
        while not self._shutdown.is_set():
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                log.exception("forwarding_probe.tick_failed", error=str(e))
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=TICK_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._shutdown.set()

    async def _tick(self) -> None:
        cutoff = datetime.now(timezone.utc) - PROBE_INTERVAL
        rows = await self.pool.fetch(
            """
            SELECT m.address, m.tenant_id,
                   gm.address AS group_address
            FROM mailboxes m
            CROSS JOIN LATERAL (
                SELECT address FROM mailboxes
                WHERE tenant_id = m.tenant_id AND is_group = TRUE AND is_active = TRUE
                  AND status IN ('direct_active','oauth_active')
                ORDER BY created_at ASC LIMIT 1
            ) gm
            WHERE m.is_active = TRUE
              AND m.is_group = FALSE
              AND m.status = 'rely_on_groups'
              AND (m.last_forwarding_probe_at IS NULL OR m.last_forwarding_probe_at < $1)
            ORDER BY COALESCE(m.last_forwarding_probe_at, m.created_at) ASC
            LIMIT 5
            """,
            cutoff,
        )
        for r in rows:
            await self._probe_one(r["address"], r["group_address"])

    async def _probe_one(self, target: str, group_addr: str) -> None:
        token = secrets.token_hex(6)
        subject = f"[codecatch-probe-{token}] forwarding test (delete me)"

        # Look up group's SMTP creds
        group_row = await self.pool.fetchrow(
            """
            SELECT mp.password_encrypted, p.smtp_host, p.smtp_port
            FROM mailboxes m
            JOIN providers p ON m.provider_id = p.id
            LEFT JOIN mailbox_passwords mp
                ON mp.mailbox_address = m.address AND mp.is_current = TRUE
            WHERE m.address = $1
            """,
            group_addr,
        )
        if not group_row or not group_row["password_encrypted"] or not group_row["smtp_host"]:
            log.debug("forwarding_probe.skip_no_smtp", group=group_addr)
            return

        try:
            password = decrypt(group_row["password_encrypted"])
        except ValueError:
            log.error("forwarding_probe.decrypt_failed", group=group_addr)
            return

        sent_at = datetime.now(timezone.utc)

        def _send():
            msg = EmailMessage()
            msg["From"] = group_addr
            msg["To"] = target
            msg["Subject"] = subject
            msg.set_content(
                f"This is an automated forwarding-test message from codecatch.\n"
                f"Token: {token}\nSent at: {sent_at.isoformat()}\n"
                f"You can safely delete this message — codecatch does not act on it.\n"
            )
            smtp_host = group_row["smtp_host"]
            smtp_port = group_row["smtp_port"] or 587
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as s:
                s.starttls()
                s.login(group_addr, password)
                s.send_message(msg)

        try:
            await asyncio.to_thread(_send)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "forwarding_probe.send_failed",
                target=target, group=group_addr, error=str(e)[:200],
            )
            await self._mark_probe(
                target, status="failed",
                error=f"SMTP send to {target}: {str(e)[:200]}",
            )
            return

        log.info("forwarding_probe.sent", target=target, group=group_addr, token=token)

        # Wait for arrival — periodically check codes table or settings
        # We're loose: look for ANY new code/row in codes with target=this address
        # received within the probe window.
        deadline = asyncio.get_event_loop().time() + PROBE_WAIT_SEC
        arrived = False
        while asyncio.get_event_loop().time() < deadline:
            n = await self.pool.fetchval(
                """
                SELECT COUNT(*) FROM codes
                WHERE target_address = $1 AND received_at > $2
                """,
                target, sent_at,
            )
            if n and n > 0:
                arrived = True
                break
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=15)
                return  # shutdown signal
            except asyncio.TimeoutError:
                pass

        if arrived:
            await self._mark_probe(target, status="ok", error=None)
            log.info("forwarding_probe.success", target=target)
        else:
            await self._mark_probe(
                target,
                status="failed",
                error=(
                    f"Sent test email at {sent_at.isoformat()}, no message arrived within "
                    f"{PROBE_WAIT_SEC}s. Forwarding may not be configured or routes elsewhere."
                ),
            )
            log.warning("forwarding_probe.failed_timeout", target=target)

    async def _mark_probe(self, address: str, *, status: str, error: str | None) -> None:
        await self.pool.execute(
            """
            UPDATE mailboxes SET
                last_forwarding_probe_at = NOW(),
                forwarding_probe_status = $2,
                forwarding_probe_error = $3
            WHERE address = $1
            """,
            address, status, error,
        )
