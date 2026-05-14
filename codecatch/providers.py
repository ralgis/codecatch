"""Provider catalogue helpers — domain → IMAP settings lookup."""
from __future__ import annotations

import asyncpg


async def resolve_provider_by_address(
    pool: asyncpg.Pool, address: str
) -> asyncpg.Record | None:
    """Find a provider whose domain_patterns contains the address's domain."""
    if "@" not in address:
        return None
    domain = address.split("@", 1)[1].lower()
    return await pool.fetchrow(
        """
        SELECT * FROM providers
        WHERE is_active = TRUE
          AND $1 = ANY(domain_patterns)
        ORDER BY array_length(domain_patterns, 1) ASC, id ASC
        LIMIT 1
        """,
        domain,
    )


async def list_active_providers(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM providers WHERE is_active = TRUE ORDER BY name"
    )
