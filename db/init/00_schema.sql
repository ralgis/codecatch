-- codecatch — initial schema.
-- Runs automatically on first start of the postgres container (empty volume).
-- For subsequent schema changes, use a migration tool — to be added.

SET client_min_messages TO WARNING;

-- ─── Tenants ────────────────────────────────────────────────────────────────
-- A tenant is a logical owner of data: mailboxes, addresses, codes, api keys.
-- For single-org self-hosted use, there's just one tenant ("default").
-- Schema-ready for multi-tenant SaaS if ever needed.
CREATE TABLE tenants (
    id           BIGSERIAL    PRIMARY KEY,
    slug         TEXT         UNIQUE NOT NULL,
    name         TEXT         NOT NULL,
    is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
    notes        TEXT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── Admins (web UI users) ──────────────────────────────────────────────────
-- Super-admins (tenant_id IS NULL) see/manage everything.
-- Tenant-admins are scoped to one tenant.
CREATE TABLE admins (
    id              BIGSERIAL    PRIMARY KEY,
    username        TEXT         UNIQUE NOT NULL,
    password_hash   TEXT         NOT NULL,                        -- bcrypt
    is_super_admin  BOOLEAN      NOT NULL DEFAULT FALSE,
    tenant_id       BIGINT       REFERENCES tenants(id) ON DELETE CASCADE,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_login_at   TIMESTAMPTZ,

    CONSTRAINT admin_scope_check CHECK (
        is_super_admin = TRUE  AND tenant_id IS NULL
        OR
        is_super_admin = FALSE AND tenant_id IS NOT NULL
    )
);

-- ─── API keys ───────────────────────────────────────────────────────────────
-- Used by audiotrace-scraper and other clients to call /api/v1.
-- We store SHA-256 hash only; the prefix is for UI display ("ccr_live_4f3b...").
CREATE TABLE api_keys (
    id              BIGSERIAL    PRIMARY KEY,
    name            TEXT         NOT NULL,
    tenant_id       BIGINT       NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key_hash        TEXT         UNIQUE NOT NULL,                 -- SHA-256 hex
    key_prefix      TEXT         NOT NULL,                        -- first 12 chars for display
    is_admin_scope  BOOLEAN      NOT NULL DEFAULT FALSE,          -- can manage tenant resources
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);
CREATE INDEX idx_api_keys_hash_active ON api_keys(key_hash) WHERE is_active = TRUE;

-- ─── Providers ──────────────────────────────────────────────────────────────
-- Pre-seeded catalogue of known email providers with their IMAP settings
-- and auth strategy. Admin can add custom providers via UI.
CREATE TABLE providers (
    id                     BIGSERIAL   PRIMARY KEY,
    name                   TEXT        UNIQUE NOT NULL,           -- 'GMX', 'Yandex', 'Microsoft', ...
    domain_patterns        TEXT[]      NOT NULL,                  -- ['gmx.de', 'gmx.com']
    imap_host              TEXT        NOT NULL,
    imap_port              INTEGER     NOT NULL DEFAULT 993,
    imap_ssl               BOOLEAN     NOT NULL DEFAULT TRUE,
    smtp_host              TEXT,
    smtp_port              INTEGER,
    auth_kind              TEXT        NOT NULL,                  -- 'basic' | 'oauth_google' | 'oauth_microsoft'
    oauth_strategy         TEXT,                                  -- 'thunderbird' | 'self_azure' | 'self_gcp' | NULL
    oauth_client_id        TEXT,
    oauth_client_secret_encrypted TEXT,                           -- for Google (their "public" secret)
    oauth_scopes           TEXT[],                                -- list of OAuth scopes to request
    notes                  TEXT,
    is_active              BOOLEAN     NOT NULL DEFAULT TRUE,
    is_builtin             BOOLEAN     NOT NULL DEFAULT FALSE,    -- pre-seeded vs user-added
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT auth_kind_check CHECK (
        auth_kind IN ('basic', 'oauth_google', 'oauth_microsoft', 'oauth_generic')
    )
);
-- GIN index for fast domain lookup
CREATE INDEX idx_providers_domains ON providers USING GIN (domain_patterns);

-- ─── Mailboxes ──────────────────────────────────────────────────────────────
-- A mailbox is one email address managed by codecatch. Strategy is decided
-- automatically based on provider + groups availability + supplied password.
CREATE TABLE mailboxes (
    address                   TEXT         PRIMARY KEY,
    tenant_id                 BIGINT       NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider_id               BIGINT       REFERENCES providers(id),

    -- Strategy / status
    status                    TEXT         NOT NULL DEFAULT 'pending',
        -- 'pending'                  : just created, not yet decided
        -- 'rely_on_groups'           : will read via group inbox(es), no direct
        -- 'direct_active'            : IMAP IDLE worker connected with stored password
        -- 'pending_oauth_headless'   : queued for Playwright OAuth attempt
        -- 'pending_oauth_manual'     : headless failed, consent_url awaits human
        -- 'oauth_active'             : OAuth flow done, refresh_token works
        -- 'invalid_credentials'      : LOGIN failed, awaits new password
        -- 'unknown_provider'         : domain not in providers DB
        -- 'no_path'                  : neither groups nor direct nor OAuth possible

    -- Group flag — central mailboxes that receive forwarded mail from many sources.
    -- A group is technically still a "direct" mailbox; the flag marks it as a
    -- shared sink that the routing engine consults for codes addressed elsewhere.
    is_group                  BOOLEAN      NOT NULL DEFAULT FALSE,

    -- Direct-IMAP IDLE worker hint: should a worker process try to keep an
    -- IDLE session for this mailbox? Only meaningful for status='direct_active'
    -- and is_group=TRUE mailboxes.
    imap_worker_enabled       BOOLEAN      NOT NULL DEFAULT TRUE,

    -- OAuth state (encrypted refresh token + metadata)
    oauth_refresh_token_encrypted TEXT,
    oauth_consented_at            TIMESTAMPTZ,
    oauth_consent_url             TEXT,             -- present when status='pending_oauth_manual'
    oauth_consent_expires_at      TIMESTAMPTZ,
    oauth_last_error              TEXT,

    -- Pending headless job tracking
    headless_attempt_count    INTEGER      NOT NULL DEFAULT 0,
    headless_last_attempt_at  TIMESTAMPTZ,
    headless_proxy_url        TEXT,                  -- optional proxy passed via API

    -- Metadata
    purpose                   TEXT,
    notes                     TEXT,
    is_active                 BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_code_at              TIMESTAMPTZ,
    last_status_check_at      TIMESTAMPTZ,
    last_error                TEXT
);
CREATE INDEX idx_mailboxes_tenant         ON mailboxes(tenant_id);
CREATE INDEX idx_mailboxes_group_active   ON mailboxes(is_group, is_active)
   WHERE is_group = TRUE AND is_active = TRUE;
CREATE INDEX idx_mailboxes_oauth_queue    ON mailboxes(status, created_at)
   WHERE status IN ('pending_oauth_headless', 'pending_oauth_manual');

-- ─── Mailbox password history ───────────────────────────────────────────────
-- One row per password ever set for a mailbox. Exactly one row has is_current=TRUE
-- at any given time (enforced by exclusion constraint).
CREATE TABLE mailbox_passwords (
    id                    BIGSERIAL    PRIMARY KEY,
    mailbox_address       TEXT         NOT NULL REFERENCES mailboxes(address) ON DELETE CASCADE,
    password_encrypted    TEXT         NOT NULL,                  -- Fernet
    is_current            BOOLEAN      NOT NULL DEFAULT FALSE,
    verified_at           TIMESTAMPTZ,                            -- last successful login with this pwd
    invalidated_at        TIMESTAMPTZ,
    invalidation_reason   TEXT,                                   -- 'replaced_by_client' | 'login_failed' | 'manual'
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- At most one current password per mailbox at any time.
CREATE UNIQUE INDEX idx_one_current_password
    ON mailbox_passwords(mailbox_address) WHERE is_current = TRUE;
CREATE INDEX idx_mailbox_passwords_addr ON mailbox_passwords(mailbox_address, created_at DESC);

-- ─── Codes ──────────────────────────────────────────────────────────────────
-- Every code extracted from an incoming email. Routing by target_address
-- regardless of which mailbox actually received the email (direct vs group).
CREATE TABLE codes (
    id                  BIGSERIAL    PRIMARY KEY,
    tenant_id           BIGINT       NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_mailbox      TEXT         REFERENCES mailboxes(address) ON DELETE SET NULL,
    target_address      TEXT         NOT NULL,                    -- from To: header
    sender              TEXT,                                     -- from From: header
    platform            TEXT,                                     -- 'tiktok' | 'instagram' | ... | NULL if unknown
    code                TEXT         NOT NULL,                    -- the extracted code
    subject             TEXT,
    body_excerpt        TEXT,                                     -- first 2KB
    message_id          TEXT,                                     -- RFC822 Message-ID for dedup
    raw_uid             TEXT,                                     -- IMAP UID
    received_at         TIMESTAMPTZ  NOT NULL,
    consumed_at         TIMESTAMPTZ,
    consumed_by_key_id  BIGINT       REFERENCES api_keys(id) ON DELETE SET NULL,
    consumed_note       TEXT
);
CREATE INDEX idx_codes_tenant_target_recv
    ON codes(tenant_id, target_address, received_at DESC);
CREATE INDEX idx_codes_unconsumed
    ON codes(tenant_id, target_address)
    WHERE consumed_at IS NULL;
-- Dedup: same Message-ID arriving via direct + group should collapse.
CREATE UNIQUE INDEX idx_codes_dedup_msgid
    ON codes(target_address, message_id)
    WHERE message_id IS NOT NULL;

-- ─── Extractor patterns ─────────────────────────────────────────────────────
-- Regex-based code extraction rules. Pre-seeded for common platforms, editable
-- via /admin/extractors. Higher priority (lower number) checked first.
CREATE TABLE extractor_patterns (
    id              BIGSERIAL    PRIMARY KEY,
    platform        TEXT         NOT NULL,                       -- 'tiktok' | 'instagram' | 'microsoft' | 'generic'
    name            TEXT         NOT NULL,
    sender_pattern  TEXT,                                        -- regex matching From: (NULL = match any)
    subject_pattern TEXT,                                        -- regex matching Subject (NULL = match any)
    code_pattern    TEXT         NOT NULL,                       -- regex with capture group → the code
    search_in       TEXT         NOT NULL DEFAULT 'body',        -- 'body' | 'subject' | 'both'
    priority        INTEGER      NOT NULL DEFAULT 100,           -- lower = checked first
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    is_builtin      BOOLEAN      NOT NULL DEFAULT FALSE,
    notes           TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_extractor_priority ON extractor_patterns(priority) WHERE is_active = TRUE;

-- ─── Settings (key-value singleton-style) ───────────────────────────────────
CREATE TABLE settings (
    key         TEXT         PRIMARY KEY,
    value       JSONB        NOT NULL,
    notes       TEXT,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── Audit log ──────────────────────────────────────────────────────────────
-- Records sensitive operations: admin login, password reveal, key creation,
-- mailbox status changes. Useful for debugging "what happened".
CREATE TABLE audit_log (
    id           BIGSERIAL    PRIMARY KEY,
    timestamp    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor_kind   TEXT         NOT NULL,                          -- 'admin' | 'api_key' | 'system'
    actor_id     TEXT,                                           -- admin username or api_key.id
    tenant_id    BIGINT       REFERENCES tenants(id) ON DELETE SET NULL,
    action       TEXT         NOT NULL,                          -- 'admin.login' | 'mailbox.password.reveal' | ...
    target_kind  TEXT,                                           -- 'mailbox' | 'api_key' | ...
    target_id    TEXT,
    ip_address   INET,
    user_agent   TEXT,
    metadata     JSONB,
    success      BOOLEAN      NOT NULL DEFAULT TRUE
);
CREATE INDEX idx_audit_timestamp ON audit_log(timestamp DESC);
CREATE INDEX idx_audit_actor     ON audit_log(actor_kind, actor_id);
CREATE INDEX idx_audit_target    ON audit_log(target_kind, target_id);

-- ─── pg_notify channel naming ───────────────────────────────────────────────
-- Workers PUBLISH 'codes_tenant_<id>' on new codes; API LISTENs to wake up
-- long-poll /codes/wait requests. No DDL needed — pg_notify is connection-based.

-- ─── End of initial schema ──────────────────────────────────────────────────
