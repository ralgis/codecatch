"""Shared logic: take a parsed message + extractor result, INSERT into codes."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import asyncpg

from codecatch.logging_setup import get_logger
from workers.extractor import ExtractionResult, run_extraction
from workers.normalizer import NormalizedMessage

log = get_logger("code_writer")

BODY_EXCERPT_BYTES = 2000


async def process_and_store(
    pool: asyncpg.Pool,
    *,
    source_mailbox_address: str,
    source_mailbox_id_unused: int | None,
    normalized: NormalizedMessage,
    received_at: datetime | None = None,
    raw_uid: str | None = None,
) -> int | None:
    """Run extraction and INSERT a code row if anything matched.

    Returns the new code id, or None if no pattern matched (we don't store
    no-code rows — they're noise). Dedups on (target_address, message_id).
    Triggers pg_notify so long-poll waiters wake up.
    """
    if not normalized.recipient:
        log.warning("code_writer.no_recipient", source=source_mailbox_address)
        return None

    async with pool.acquire() as conn:
        # Resolve owning tenant via the target mailbox (the address mail was sent TO).
        # If it's not registered for any tenant we still want to record codes for
        # diagnostic visibility — store under the source mailbox's tenant.
        target_tenant = await conn.fetchval(
            "SELECT tenant_id FROM mailboxes WHERE address = $1",
            normalized.recipient,
        )
        source_tenant = await conn.fetchval(
            "SELECT tenant_id FROM mailboxes WHERE address = $1",
            source_mailbox_address,
        )
        tenant_id = target_tenant or source_tenant
        if tenant_id is None:
            log.warning(
                "code_writer.no_tenant",
                target=normalized.recipient, source=source_mailbox_address,
            )
            return None

        patterns = await conn.fetch(
            "SELECT * FROM extractor_patterns WHERE is_active = TRUE ORDER BY priority"
        )
        result: ExtractionResult = run_extraction(
            sender=normalized.sender,
            subject=normalized.subject,
            body=normalized.body_text,
            patterns=patterns,
        )
        if result.code is None:
            log.info(
                "code_writer.no_match",
                target=normalized.recipient, sender=normalized.sender[:60],
            )
            return None

        excerpt = normalized.body_text[:BODY_EXCERPT_BYTES]
        try:
            new_id = await conn.fetchval(
                """
                INSERT INTO codes (
                    tenant_id, source_mailbox, target_address, sender,
                    platform, code, subject, body_excerpt, message_id,
                    raw_uid, received_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (target_address, message_id) WHERE message_id IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                tenant_id, source_mailbox_address, normalized.recipient, normalized.sender,
                result.platform, result.code, normalized.subject, excerpt, normalized.message_id,
                raw_uid, received_at or datetime.now(timezone.utc),
            )
        except asyncpg.UniqueViolationError:
            new_id = None

        if new_id is None:
            log.info(
                "code_writer.duplicate",
                target=normalized.recipient, message_id=normalized.message_id,
            )
            return None

        await conn.execute(
            "UPDATE mailboxes SET last_code_at = NOW() WHERE address = $1",
            normalized.recipient,
        )

        payload = json.dumps({"code_id": new_id, "target_address": normalized.recipient})
        await conn.execute(f"SELECT pg_notify('codes_tenant_{tenant_id}', $1)", payload)

    log.info(
        "code_writer.stored",
        code_id=new_id, target=normalized.recipient,
        code=result.code, platform=result.platform,
    )
    return new_id
