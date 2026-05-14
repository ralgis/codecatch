"""Mailbox CRUD + strategy selection.

Centralised because the same logic runs from both the admin UI form and the
REST API POST /mailboxes endpoint. Whoever calls us provides:
    address, tenant_id, provider_id, password (optional), is_group, ...
We:
  1. Upsert the mailbox row.
  2. If a password was provided and differs from current — store as new
     current, invalidate previous.
  3. Decide strategy:
       - has groups in tenant AND provider is oauth-only → rely_on_groups
       - provider is basic + password → try IMAP login → direct_active /
         invalid_credentials
       - provider is oauth + password → queue for headless consent
       - no path → no_path
  4. Update mailbox.status accordingly.
"""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from codecatch.crypto import decrypt, encrypt
from codecatch.logging_setup import get_logger

log = get_logger("mailbox_service")


@dataclass
class UpsertResult:
    address: str
    status: str
    is_new: bool
    password_changed: bool
    note: str | None = None


class MailboxError(ValueError):
    pass


async def upsert_mailbox(
    pool: asyncpg.Pool,
    *,
    address: str,
    tenant_id: int,
    provider_id: int | None,
    password: str | None,
    is_group: bool = False,
    purpose: str = "",
    notes: str = "",
    proxy_url: str | None = None,
) -> UpsertResult:
    address = address.strip().lower()
    if "@" not in address:
        raise MailboxError("Invalid email address")
    if provider_id is None:
        raise MailboxError(f"Unknown provider for domain — register it under /admin/providers")

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM mailboxes WHERE address = $1", address
            )
            is_new = existing is None

            if is_new:
                await conn.execute(
                    """
                    INSERT INTO mailboxes (
                        address, tenant_id, provider_id, is_group,
                        purpose, notes, headless_proxy_url, status
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
                    """,
                    address, tenant_id, provider_id, is_group,
                    purpose, notes, proxy_url,
                )
            else:
                # Refresh metadata but preserve tenant_id (it's the owner).
                if existing["tenant_id"] != tenant_id:
                    raise MailboxError("Mailbox belongs to a different tenant")
                await conn.execute(
                    """
                    UPDATE mailboxes
                    SET provider_id = $2, is_group = $3, purpose = $4, notes = $5,
                        headless_proxy_url = COALESCE($6, headless_proxy_url),
                        updated_at = NOW()
                    WHERE address = $1
                    """,
                    address, provider_id, is_group, purpose, notes, proxy_url,
                )

            password_changed = False
            if password:
                current_pw_row = await conn.fetchrow(
                    """
                    SELECT id, password_encrypted FROM mailbox_passwords
                    WHERE mailbox_address = $1 AND is_current = TRUE
                    """,
                    address,
                )
                if current_pw_row is None:
                    await _insert_password(conn, address, password, is_current=True)
                    password_changed = True
                else:
                    try:
                        current_plain = decrypt(current_pw_row["password_encrypted"])
                    except ValueError:
                        current_plain = None  # corrupted — treat as changed
                    if current_plain != password:
                        await conn.execute(
                            """
                            UPDATE mailbox_passwords
                            SET is_current = FALSE,
                                invalidated_at = NOW(),
                                invalidation_reason = 'replaced_by_client'
                            WHERE id = $1
                            """,
                            current_pw_row["id"],
                        )
                        await _insert_password(conn, address, password, is_current=True)
                        password_changed = True

            # Provider for strategy decision
            provider = await conn.fetchrow(
                "SELECT * FROM providers WHERE id = $1", provider_id
            )
            status, note = await _decide_status(conn, address, tenant_id, provider, has_password=bool(password))
            await conn.execute(
                "UPDATE mailboxes SET status = $2, updated_at = NOW() WHERE address = $1",
                address, status,
            )

    log.info(
        "mailbox.upsert",
        address=address, status=status, is_new=is_new, password_changed=password_changed,
    )
    return UpsertResult(
        address=address, status=status, is_new=is_new,
        password_changed=password_changed, note=note,
    )


async def _insert_password(conn: asyncpg.Connection, address: str, password: str, is_current: bool) -> None:
    await conn.execute(
        """
        INSERT INTO mailbox_passwords (mailbox_address, password_encrypted, is_current)
        VALUES ($1, $2, $3)
        """,
        address, encrypt(password), is_current,
    )


async def _decide_status(
    conn: asyncpg.Connection,
    address: str,
    tenant_id: int,
    provider: asyncpg.Record | None,
    has_password: bool,
) -> tuple[str, str | None]:
    if provider is None:
        return "unknown_provider", "Domain not in providers catalogue"

    # If THIS mailbox is itself a group → it must have direct creds, treat as direct.
    is_group = await conn.fetchval("SELECT is_group FROM mailboxes WHERE address = $1", address)
    if is_group:
        if has_password and provider["auth_kind"] == "basic":
            return "direct_active", "Group inbox — IMAP IDLE will start"
        if provider["auth_kind"] in ("oauth_google", "oauth_microsoft"):
            return "pending_oauth_headless", "Group inbox uses OAuth — queued for consent"
        return "no_path", "Group inbox requires either basic-auth password or OAuth flow"

    # Non-group mailboxes
    has_groups = await conn.fetchval(
        """
        SELECT EXISTS(
            SELECT 1 FROM mailboxes
            WHERE tenant_id = $1 AND is_group = TRUE AND is_active = TRUE
              AND status IN ('direct_active', 'oauth_active')
        )
        """,
        tenant_id,
    )
    auth = provider["auth_kind"]

    if auth == "basic":
        if has_password:
            return "direct_active", "Direct IMAP login pending verification by worker"
        if has_groups:
            return "rely_on_groups", "No password — will rely on group forwarding"
        return "no_path", "No password and no group inbox configured"

    # OAuth provider
    if has_groups:
        return "rely_on_groups", "OAuth provider with groups available — using forwarding"
    if has_password:
        return "pending_oauth_headless", "OAuth provider — queued for headless consent"
    return "no_path", "OAuth provider with no password and no group inbox"
