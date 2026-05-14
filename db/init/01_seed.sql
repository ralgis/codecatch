-- codecatch — seed data (providers + extractor patterns).
-- Loaded after 00_schema.sql on first postgres init.

-- ─── Providers ──────────────────────────────────────────────────────────────
-- All commonly seen consumer email providers. auth_kind drives the strategy:
-- 'basic' → plain IMAP LOGIN with stored password (Yandex, GMX, Mail.ru, ...).
-- 'oauth_google' / 'oauth_microsoft' → OAuth2 flow (Gmail, Outlook).
--
-- For OAuth providers we ship Mozilla Thunderbird's publicly-known client_ids
-- to let codecatch work out of the box without Azure/GCP registration.
-- Replace with your own ('self_azure' / 'self_gcp') in production at scale.

INSERT INTO providers (name, domain_patterns, imap_host, imap_port, imap_ssl, smtp_host, smtp_port,
                       auth_kind, oauth_strategy, oauth_client_id, oauth_scopes, is_builtin, notes)
VALUES
    -- ── Basic-auth IMAP, still works in 2026 ────────────────────────────────
    ('GMX (DE)',
     ARRAY['gmx.de', 'gmx.net', 'gmx.at', 'gmx.ch'],
     'imap.gmx.net', 993, TRUE, 'mail.gmx.net', 587,
     'basic', NULL, NULL, NULL, TRUE,
     'IMAP must be enabled in web settings → POP3/IMAP Abruf.'),

    ('GMX (international)',
     ARRAY['gmx.com'],
     'imap.gmx.com', 993, TRUE, 'mail.gmx.com', 587,
     'basic', NULL, NULL, NULL, TRUE,
     NULL),

    ('Yandex',
     ARRAY['yandex.com', 'yandex.ru', 'ya.ru'],
     'imap.yandex.com', 993, TRUE, 'smtp.yandex.com', 465,
     'basic', NULL, NULL, NULL, TRUE,
     'Generate app-password if 2FA enabled. Standard password may be blocked.'),

    ('Mail.ru',
     ARRAY['mail.ru', 'inbox.ru', 'list.ru', 'bk.ru', 'internet.ru'],
     'imap.mail.ru', 993, TRUE, 'smtp.mail.ru', 465,
     'basic', NULL, NULL, NULL, TRUE,
     NULL),

    ('Yahoo',
     ARRAY['yahoo.com', 'yahoo.de', 'yahoo.co.uk', 'ymail.com', 'rocketmail.com'],
     'imap.mail.yahoo.com', 993, TRUE, 'smtp.mail.yahoo.com', 465,
     'basic', NULL, NULL, NULL, TRUE,
     'Requires app password (Yahoo Account → Security → Generate app password).'),

    ('Web.de',
     ARRAY['web.de'],
     'imap.web.de', 993, TRUE, 'smtp.web.de', 587,
     'basic', NULL, NULL, NULL, TRUE,
     'Owned by United Internet (same group as GMX). Enable POP3/IMAP in settings.'),

    ('T-Online',
     ARRAY['t-online.de', 'magenta.de'],
     'secureimap.t-online.de', 993, TRUE, 'securesmtp.t-online.de', 465,
     'basic', NULL, NULL, NULL, TRUE,
     NULL),

    ('iCloud',
     ARRAY['icloud.com', 'me.com', 'mac.com'],
     'imap.mail.me.com', 993, TRUE, 'smtp.mail.me.com', 587,
     'basic', NULL, NULL, NULL, TRUE,
     'Requires Apple ID app-specific password (appleid.apple.com → Sign-In and Security).'),

    ('Migadu',
     ARRAY['migadu.com'],
     'imap.migadu.com', 993, TRUE, 'smtp.migadu.com', 465,
     'basic', NULL, NULL, NULL, TRUE,
     'Paid hosted email. Use full email as username.'),

    -- ── OAuth-only providers ────────────────────────────────────────────────
    ('Google',
     ARRAY['gmail.com', 'googlemail.com'],
     'imap.gmail.com', 993, TRUE, 'smtp.gmail.com', 465,
     'oauth_google', 'thunderbird',
     '406964657835-aq8lmia8j95dhl1a2bvharmfk3t1hgqj.apps.googleusercontent.com',
     ARRAY['https://mail.google.com/'],
     TRUE,
     'Uses Mozilla Thunderbird public client_id. Replace with own GCP-registered app for production scale.'),

    ('Microsoft',
     ARRAY['outlook.com', 'hotmail.com', 'live.com', 'msn.com', 'outlook.de', 'hotmail.de', 'live.de'],
     'outlook.office365.com', 993, TRUE, 'smtp-mail.outlook.com', 587,
     'oauth_microsoft', 'thunderbird',
     '9e5f94bc-e8a4-4e73-b8be-63364c29d753',
     ARRAY['offline_access', 'https://outlook.office.com/IMAP.AccessAsUser.All', 'https://outlook.office.com/SMTP.Send'],
     TRUE,
     'Uses Mozilla Thunderbird public client_id. Basic auth is permanently disabled by Microsoft (April 2026).');

-- ─── Extractor patterns ─────────────────────────────────────────────────────
-- Priority 10-50: tight, sender-specific. Generic fallback at 1000.

INSERT INTO extractor_patterns (platform, name, sender_pattern, subject_pattern, code_pattern, search_in, priority, is_builtin, notes)
VALUES
    -- ── TikTok ──────────────────────────────────────────────────────────────
    ('tiktok', 'TikTok 6-digit code',
     '(?i)tiktok\.com|noreply@account\.tiktok',
     NULL,
     '(?<![\d])(\d{6})(?![\d])',
     'subject', 10, TRUE,
     'TikTok puts the 6-digit code in the subject ("646207 is your 6-digit code").'),

    -- ── Instagram ───────────────────────────────────────────────────────────
    ('instagram', 'Instagram security code',
     '(?i)mail\.instagram\.com|no-reply@(mail\.)?instagram',
     NULL,
     '(?:security code is|verification code is|code is)\s*[:\-]?\s*(\d{4,8})',
     'both', 10, TRUE,
     'Instagram phrases vary; usually "Your Instagram security code is 524117".'),

    ('instagram', 'Instagram subject code',
     '(?i)mail\.instagram\.com|no-reply@(mail\.)?instagram',
     '(?i)code',
     '(?<![\d])(\d{6})(?![\d])',
     'subject', 11, TRUE,
     'Fallback if body extraction missed: subject often contains the code too.'),

    -- ── Microsoft Account ───────────────────────────────────────────────────
    ('microsoft', 'Microsoft security code (EN/RU)',
     '(?i)account-security-noreply@accountprotection\.microsoft\.com|@accountprotection',
     NULL,
     '(?:(?:Security|Verification)\s*code|Код\s*безопасности)\s*[:\-]?\s*(\d{4,8})',
     'body', 10, TRUE,
     'Matches both English "Security code: 135399" and Russian "Код безопасности: 135399".'),

    -- ── Twitter / X ─────────────────────────────────────────────────────────
    ('twitter', 'X / Twitter code',
     '(?i)@x\.com|@twitter\.com|info@x\.com',
     NULL,
     '(?<![\d])(\d{6,8})(?![\d])',
     'both', 20, TRUE,
     NULL),

    -- ── Facebook / Meta ─────────────────────────────────────────────────────
    ('facebook', 'Facebook code',
     '(?i)facebook(?:mail)?\.com|notification@facebookmail\.com',
     NULL,
     '(?:FB-\s*)?(\d{5,8})\s*(?:is your|verification)',
     'body', 20, TRUE,
     NULL),

    -- ── Generic fallback ────────────────────────────────────────────────────
    ('generic', 'Generic 4-8 digit number in subject',
     NULL, NULL,
     '(?<![\d])(\d{4,8})(?![\d])',
     'subject', 1000, TRUE,
     'Last-resort: any 4-8 digit number in subject. Use only when sender-specific patterns missed.'),

    ('generic', 'Generic 4-8 digit number in body',
     NULL, NULL,
     '(?:code|pin|otp)\s*[:\-]?\s*(\d{4,8})',
     'body', 1010, TRUE,
     'Last-resort: looks for "code: NNN" / "pin: NNN" / "otp: NNN" in body.');

-- ─── Bootstrap is performed by the API on first start, not by SQL seed:
--   - default tenant "default"
--   - super-admin from BOOTSTRAP_ADMIN_USER/PASSWORD env
--   - one bootstrap API-key (printed to logs once)
-- See api/bootstrap.py (to be added).
