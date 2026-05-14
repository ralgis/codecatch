-- Migration 0002: per-mailbox `mode` and explicit forwarding_target.
-- Runs idempotently — safe to apply on top of an existing schema.

ALTER TABLE mailboxes
    ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'auto'
        CHECK (mode IN ('auto', 'direct_only', 'group_only', 'both'));

ALTER TABLE mailboxes
    ADD COLUMN IF NOT EXISTS forwarding_target TEXT;

-- Latest OAuth access_token cache so we don't refresh on every request.
ALTER TABLE mailboxes
    ADD COLUMN IF NOT EXISTS oauth_access_token_encrypted TEXT;

ALTER TABLE mailboxes
    ADD COLUMN IF NOT EXISTS oauth_access_token_expires_at TIMESTAMPTZ;

-- last forwarding probe state (active probe feature)
ALTER TABLE mailboxes
    ADD COLUMN IF NOT EXISTS last_forwarding_probe_at TIMESTAMPTZ;

ALTER TABLE mailboxes
    ADD COLUMN IF NOT EXISTS forwarding_probe_status TEXT
        CHECK (forwarding_probe_status IN ('ok', 'failed', 'pending') OR forwarding_probe_status IS NULL);

ALTER TABLE mailboxes
    ADD COLUMN IF NOT EXISTS forwarding_probe_error TEXT;
