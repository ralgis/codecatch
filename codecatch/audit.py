"""Audit-log helper — single entry point so call sites don't construct rows."""
from __future__ import annotations

import json
from typing import Any

import asyncpg


async def write_audit(
    conn_or_pool: asyncpg.Connection | asyncpg.Pool,
    *,
    action: str,
    actor_kind: str,                    # 'admin' | 'api_key' | 'system'
    actor_id: str | None = None,
    tenant_id: int | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    metadata: dict[str, Any] | None = None,
    success: bool = True,
) -> None:
    meta_json = json.dumps(metadata) if metadata else None
    query = """
        INSERT INTO audit_log (
            actor_kind, actor_id, tenant_id, action,
            target_kind, target_id, ip_address, user_agent,
            metadata, success
        ) VALUES ($1, $2, $3, $4, $5, $6, $7::inet, $8, $9::jsonb, $10)
    """
    args = (
        actor_kind, actor_id, tenant_id, action,
        target_kind, target_id, ip_address, user_agent,
        meta_json, success,
    )
    if isinstance(conn_or_pool, asyncpg.Pool):
        async with conn_or_pool.acquire() as conn:
            await conn.execute(query, *args)
    else:
        await conn_or_pool.execute(query, *args)
