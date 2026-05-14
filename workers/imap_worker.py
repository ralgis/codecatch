"""One IMAP IDLE session per active direct mailbox.

Architecture:
  - The `WorkerManager` polls the DB for mailboxes needing a worker
    (is_group=TRUE and status='direct_active', OR is_group=FALSE and
    status='direct_active'), and ensures each has a running asyncio task.
  - Each per-mailbox task opens an IMAP4_SSL connection, LOGINs with the
    stored password, IDLEs on INBOX, processes any new messages, and
    reconnects on errors with exponential backoff.
  - On UID FETCH we hand the raw RFC822 bytes to `workers.normalizer` →
    `workers.code_writer` for storage + pg_notify.

Uses imap-tools (synchronous, but we wrap blocking calls in to_thread). For
a few dozen mailboxes this is comfortable; if it ever grows past ~200 we'd
swap to aioimaplib for true async.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import asyncpg
from imap_tools import MailBox, MailMessage

from codecatch.crypto import decrypt
from codecatch.logging_setup import get_logger
from workers.code_writer import process_and_store
from workers.normalizer import parse_rfc822

log = get_logger("imap_worker")

IDLE_TIMEOUT_SEC = 29 * 60       # IMAP IDLE servers typically cut at 29 min
BACKOFF_MIN = 5
BACKOFF_MAX = 300
LAST_SEEN_UID_KEY = "imap.last_seen_uid"


@dataclass
class MailboxConfig:
    address: str
    provider_host: str
    provider_port: int
    provider_ssl: bool
    password: str
    is_group: bool = False


@dataclass
class WorkerState:
    config: MailboxConfig
    task: asyncio.Task
    last_started: float = field(default_factory=time.time)


class ImapWorkerManager:
    def __init__(self, pool: asyncpg.Pool, max_workers: int = 50):
        self.pool = pool
        self.max_workers = max_workers
        self._workers: dict[str, WorkerState] = {}
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        log.info("imap_manager.start", max_workers=self.max_workers)
        while not self._shutdown.is_set():
            try:
                await self.reconcile_workers()
            except Exception as e:  # noqa: BLE001
                log.exception("imap_manager.reconcile_failed", error=str(e))
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

        await self.stop_all()

    async def stop(self) -> None:
        self._shutdown.set()

    async def stop_all(self) -> None:
        for state in list(self._workers.values()):
            state.task.cancel()
        await asyncio.gather(
            *[state.task for state in self._workers.values()],
            return_exceptions=True,
        )
        self._workers.clear()

    async def reconcile_workers(self) -> None:
        """Bring the set of running per-mailbox tasks in line with DB state."""
        rows = await self.pool.fetch(
            """
            SELECT
                m.address, m.is_group, m.is_active, m.status, m.imap_worker_enabled,
                p.imap_host, p.imap_port, p.imap_ssl, p.auth_kind,
                mp.password_encrypted
            FROM mailboxes m
            JOIN providers p ON m.provider_id = p.id
            LEFT JOIN mailbox_passwords mp ON mp.mailbox_address = m.address AND mp.is_current = TRUE
            WHERE m.is_active = TRUE
              AND m.imap_worker_enabled = TRUE
              AND p.auth_kind = 'basic'
              AND m.status = 'direct_active'
              AND mp.password_encrypted IS NOT NULL
            ORDER BY m.is_group DESC, m.created_at ASC
            LIMIT $1
            """,
            self.max_workers,
        )

        wanted: dict[str, MailboxConfig] = {}
        for r in rows:
            try:
                pw = decrypt(r["password_encrypted"])
            except ValueError as e:
                log.error(
                    "imap_manager.decrypt_failed", address=r["address"], error=str(e),
                )
                continue
            wanted[r["address"]] = MailboxConfig(
                address=r["address"],
                provider_host=r["imap_host"],
                provider_port=r["imap_port"],
                provider_ssl=r["imap_ssl"],
                password=pw,
                is_group=r["is_group"],
            )

        # Stop workers no longer in `wanted`.
        for addr in list(self._workers.keys()):
            if addr not in wanted:
                log.info("imap_manager.stop_worker", address=addr)
                self._workers[addr].task.cancel()
                del self._workers[addr]

        # Start workers for new entries.
        for addr, cfg in wanted.items():
            existing = self._workers.get(addr)
            if existing and not existing.task.done():
                continue
            if existing and existing.task.done():
                # task crashed — restart with fresh state
                log.warning("imap_manager.restart_dead_worker", address=addr)
            task = asyncio.create_task(self._run_one(cfg), name=f"imap:{addr}")
            self._workers[addr] = WorkerState(config=cfg, task=task)
            log.info("imap_manager.start_worker", address=addr, is_group=cfg.is_group)

    async def _run_one(self, cfg: MailboxConfig) -> None:
        backoff = BACKOFF_MIN
        while not self._shutdown.is_set():
            try:
                await self._session_loop(cfg)
                # Clean reconnect = reset backoff
                backoff = BACKOFF_MIN
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("imap_worker.error", address=cfg.address, error=str(e))
                await self._record_error(cfg.address, str(e)[:500])
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _session_loop(self, cfg: MailboxConfig) -> None:
        """One full IMAP login → fetch backlog → IDLE → disconnect cycle."""
        def _open_box() -> MailBox:
            box = MailBox(cfg.provider_host, port=cfg.provider_port)
            box.login(cfg.address, cfg.password, initial_folder="INBOX")
            return box

        box = await asyncio.to_thread(_open_box)
        log.info("imap_worker.connected", address=cfg.address)
        try:
            await self._mark_verified(cfg.address)

            # 1) Fetch any unseen backlog since last_seen_uid.
            await self._fetch_backlog(box, cfg)

            # 2) Enter IDLE loop with periodic re-IDLE.
            await self._idle_loop(box, cfg)
        finally:
            try:
                await asyncio.to_thread(box.logout)
            except Exception:  # noqa: BLE001
                pass

    async def _fetch_backlog(self, box: MailBox, cfg: MailboxConfig) -> None:
        """Pull messages with UID > last_seen_uid (or newest 20 on first run)."""
        last_uid = await self._get_last_seen_uid(cfg.address)
        criteria = f"UID {int(last_uid) + 1}:*" if last_uid else "ALL"
        limit = None if last_uid else 20  # cap initial run

        def _fetch():
            return list(box.fetch(criteria=criteria, limit=limit, mark_seen=False, reverse=False))

        try:
            messages: list[MailMessage] = await asyncio.to_thread(_fetch)
        except Exception as e:  # noqa: BLE001
            log.warning("imap_worker.backlog_fetch_failed", address=cfg.address, error=str(e))
            return

        for m in messages:
            await self._process_message(cfg, m)
            try:
                uid_int = int(m.uid)
            except (TypeError, ValueError):
                continue
            await self._update_last_seen_uid(cfg.address, uid_int)

    async def _idle_loop(self, box: MailBox, cfg: MailboxConfig) -> None:
        while not self._shutdown.is_set():
            def _idle():
                # imap_tools idle: returns list of responses on changes / timeout
                with box.idle as idle:
                    return idle.wait(timeout=IDLE_TIMEOUT_SEC)

            try:
                responses = await asyncio.to_thread(_idle)
            except Exception as e:  # noqa: BLE001
                log.warning("imap_worker.idle_failed", address=cfg.address, error=str(e))
                raise

            if not responses:
                # IDLE timeout — refresh connection
                continue

            # Something arrived — fetch new messages
            await self._fetch_backlog(box, cfg)

    async def _process_message(self, cfg: MailboxConfig, m: MailMessage) -> None:
        try:
            normalized = parse_rfc822(m.obj.as_bytes())
            await process_and_store(
                self.pool,
                source_mailbox_address=cfg.address,
                source_mailbox_id_unused=None,
                normalized=normalized,
                received_at=m.date,
                raw_uid=str(m.uid) if m.uid else None,
            )
        except Exception as e:  # noqa: BLE001
            log.exception(
                "imap_worker.process_failed",
                address=cfg.address, uid=m.uid, error=str(e),
            )

    # ── persistence helpers ────────────────────────────────────────────────
    async def _get_last_seen_uid(self, address: str) -> int | None:
        key = f"{LAST_SEEN_UID_KEY}.{address}"
        row = await self.pool.fetchrow("SELECT value FROM settings WHERE key = $1", key)
        if not row:
            return None
        try:
            v = row["value"]
            if isinstance(v, dict):
                return int(v.get("uid", 0)) or None
            return int(v) or None
        except (TypeError, ValueError):
            return None

    async def _update_last_seen_uid(self, address: str, uid: int) -> None:
        import json
        key = f"{LAST_SEEN_UID_KEY}.{address}"
        await self.pool.execute(
            """
            INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            key, json.dumps({"uid": uid}),
        )

    async def _mark_verified(self, address: str) -> None:
        await self.pool.execute(
            """
            UPDATE mailbox_passwords SET verified_at = NOW()
            WHERE mailbox_address = $1 AND is_current = TRUE
            """,
            address,
        )
        await self.pool.execute(
            "UPDATE mailboxes SET last_status_check_at = NOW(), last_error = NULL WHERE address = $1",
            address,
        )

    async def _record_error(self, address: str, error: str) -> None:
        await self.pool.execute(
            """
            UPDATE mailboxes SET last_error = $2, last_status_check_at = NOW()
            WHERE address = $1
            """,
            address, error,
        )
