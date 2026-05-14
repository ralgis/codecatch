"""First-run bootstrap: ensure default tenant + super-admin + initial API key.

Called on API startup. Idempotent — if already done, just verifies state.
Prints the bootstrap API key to logs **once** on first run; if you lose it,
create a new one via the admin UI.
"""
from __future__ import annotations

import asyncpg

from codecatch.config import get_settings
from codecatch.crypto import generate_api_key, hash_password
from codecatch.logging_setup import get_logger

log = get_logger("bootstrap")

DEFAULT_TENANT_SLUG = "default"
DEFAULT_TENANT_NAME = "Default"
BOOTSTRAP_API_KEY_NAME = "bootstrap"


async def run_bootstrap(pool: asyncpg.Pool) -> None:
    s = get_settings()

    async with pool.acquire() as conn:
        async with conn.transaction():
            tenant_id = await _ensure_default_tenant(conn)
            admin_created = await _ensure_super_admin(
                conn, s.bootstrap_admin_user, s.bootstrap_admin_password
            )
            api_key_token = await _ensure_bootstrap_api_key(conn, tenant_id)

    if admin_created:
        log.warning(
            "bootstrap.super_admin_created",
            username=s.bootstrap_admin_user,
            note="change password after first login",
        )
    if api_key_token:
        # We only have the cleartext on first creation. After that we only
        # store the hash. So if you lose this — create a new key in admin UI.
        log.warning(
            "bootstrap.api_key_created",
            name=BOOTSTRAP_API_KEY_NAME,
            token=api_key_token,
            note="SAVE THIS TOKEN NOW — it cannot be retrieved later",
        )


async def _ensure_default_tenant(conn: asyncpg.Connection) -> int:
    row = await conn.fetchrow("SELECT id FROM tenants WHERE slug = $1", DEFAULT_TENANT_SLUG)
    if row:
        return row["id"]
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (slug, name) VALUES ($1, $2) RETURNING id",
        DEFAULT_TENANT_SLUG,
        DEFAULT_TENANT_NAME,
    )
    log.info("bootstrap.tenant_created", slug=DEFAULT_TENANT_SLUG, id=tenant_id)
    return tenant_id


async def _ensure_super_admin(conn: asyncpg.Connection, username: str, password: str) -> bool:
    """Return True iff we created the admin in this call."""
    existing = await conn.fetchval(
        "SELECT 1 FROM admins WHERE is_super_admin = TRUE LIMIT 1"
    )
    if existing:
        return False
    await conn.execute(
        """
        INSERT INTO admins (username, password_hash, is_super_admin, tenant_id, is_active)
        VALUES ($1, $2, TRUE, NULL, TRUE)
        """,
        username,
        hash_password(password),
    )
    return True


async def _ensure_bootstrap_api_key(conn: asyncpg.Connection, tenant_id: int) -> str | None:
    """Return the clear-text token iff we created one in this call, else None."""
    existing = await conn.fetchval(
        "SELECT 1 FROM api_keys WHERE name = $1 AND tenant_id = $2",
        BOOTSTRAP_API_KEY_NAME,
        tenant_id,
    )
    if existing:
        return None

    token, prefix, key_hash = generate_api_key()
    await conn.execute(
        """
        INSERT INTO api_keys (name, tenant_id, key_hash, key_prefix, is_admin_scope, is_active)
        VALUES ($1, $2, $3, $4, TRUE, TRUE)
        """,
        BOOTSTRAP_API_KEY_NAME,
        tenant_id,
        key_hash,
        prefix,
    )
    return token
