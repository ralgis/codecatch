"""Workers entrypoint — runs IMAP IDLE manager + OAuth headless worker concurrently."""
from __future__ import annotations

import asyncio
import signal

from codecatch.config import get_settings
from codecatch.db import create_pool
from codecatch.logging_setup import configure_logging, get_logger
from workers.forwarding_probe import ForwardingProbeWorker
from workers.imap_worker import ImapWorkerManager
from workers.oauth_refresh import OAuthRefresher
from workers.oauth_worker import OAuthWorker


async def amain() -> None:
    s = get_settings()
    configure_logging(s.log_level)
    log = get_logger("workers.main")
    log.info("workers.startup", max_imap=s.max_imap_workers)

    pool = await create_pool(min_size=2, max_size=15)

    imap = ImapWorkerManager(pool, max_workers=s.max_imap_workers)
    oauth = OAuthWorker(pool)
    oauth_refresh = OAuthRefresher(pool)
    probe = ForwardingProbeWorker(pool)

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            # Windows in some configurations: signal handlers not available
            pass

    tasks = [
        asyncio.create_task(imap.run(), name="imap_manager"),
        asyncio.create_task(oauth.run(), name="oauth_worker"),
        asyncio.create_task(oauth_refresh.run(), name="oauth_refresh"),
        asyncio.create_task(probe.run(), name="forwarding_probe"),
    ]

    await shutdown.wait()
    log.info("workers.shutdown_requested")

    await imap.stop()
    await oauth.stop()
    await oauth_refresh.stop()
    await probe.stop()

    await asyncio.gather(*tasks, return_exceptions=True)
    await pool.close()
    log.info("workers.shutdown_complete")


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
