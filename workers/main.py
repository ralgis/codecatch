"""Workers entrypoint — minimal scaffold.

Real worker loop (IMAP IDLE per active mailbox, OAuth headless queue, code
extraction) is implemented in subsequent commits. For now this process:
  - connects to Postgres
  - logs that it's alive
  - sleeps in a loop with periodic heartbeat to prove the container is healthy

Run with:
    python -m workers.main
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

import asyncpg


LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
DATABASE_URL = os.environ.get("DATABASE_URL", "")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [workers] %(message)s",
)
log = logging.getLogger(__name__)


class Workers:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None
        self.shutdown_event = asyncio.Event()

    async def start(self) -> None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is required")
        self.pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=10, command_timeout=10
        )
        log.info("Postgres pool ready")

        # Install signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.shutdown_event.set)

        await self.heartbeat_loop()

    async def heartbeat_loop(self) -> None:
        """Placeholder loop — proves the container is alive.
        Real worker logic (IMAP IDLE, OAuth jobs) goes here."""
        assert self.pool is not None
        while not self.shutdown_event.is_set():
            try:
                async with self.pool.acquire() as conn:
                    n_mailboxes = await conn.fetchval(
                        "SELECT COUNT(*) FROM mailboxes WHERE is_active = TRUE"
                    )
                    n_codes = await conn.fetchval("SELECT COUNT(*) FROM codes")
                log.info(
                    "heartbeat: mailboxes=%s codes=%s (workers idle, no real logic yet)",
                    n_mailboxes,
                    n_codes,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat query failed: %s", e)

            try:
                await asyncio.wait_for(self.shutdown_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

        log.info("shutdown signal received, cleaning up")
        await self.pool.close()
        log.info("workers stopped")


def main() -> None:
    workers = Workers()
    try:
        asyncio.run(workers.start())
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
