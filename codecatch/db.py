"""Async Postgres pool + helpers.

Pool lifecycle is managed by whatever process imports this module — for the
API that's FastAPI's lifespan, for workers it's the main loop. Use
`acquire()` directly in handlers/jobs, and rely on transactions where needed.
"""
from __future__ import annotations

import asyncpg

from codecatch.config import get_settings


async def create_pool(min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    s = get_settings()
    return await asyncpg.create_pool(
        s.database_url,
        min_size=min_size,
        max_size=max_size,
        command_timeout=15,
        # Decode TEXT[] columns into Python lists, JSONB into dicts — defaults.
    )


async def fetch_one(pool: asyncpg.Pool, query: str, *args) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetch_all(pool: asyncpg.Pool, query: str, *args) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def execute(pool: asyncpg.Pool, query: str, *args) -> str:
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)
