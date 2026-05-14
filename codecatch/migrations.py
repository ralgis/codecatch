"""Tiny migration runner.

Applies any *.sql file in db/migrations/ in numeric order, tracking applied
ones in a `schema_migrations` table. Called once on API startup, after the
initial-schema seed has had its chance to run.

Files are expected to be idempotent (using IF NOT EXISTS, ON CONFLICT etc.).
"""
from __future__ import annotations

import re
from pathlib import Path

import asyncpg

from codecatch.logging_setup import get_logger

log = get_logger("migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


async def run_migrations(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}

        files = sorted(MIGRATIONS_DIR.glob("*.sql")) if MIGRATIONS_DIR.exists() else []
        for f in files:
            m = re.match(r"^(\d+)_", f.name)
            if not m:
                continue
            version = m.group(1)
            if version in applied:
                continue

            log.info("migration.apply", version=version, file=f.name)
            sql = f.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", version
                )
            log.info("migration.applied", version=version)
