# codecatch

Self-hosted service for receiving and routing email verification codes (TikTok,
Instagram, Microsoft, Google, etc.) across many mailboxes — exposed as a simple
HTTP API for downstream automation.

## Why this exists

Modern consumer email providers (Outlook.com / Hotmail, Gmail) have disabled
basic-auth IMAP in 2024-2026. Account-creation workflows that need to read
verification codes are stuck choosing between:

- registering OAuth apps per provider (Azure, Google Cloud) — slow setup, ToS
  friction, per-account consent UX;
- forwarding everything to a central mailbox at a provider that still supports
  basic auth (GMX, Yandex, mail.ru, ...) — works, but you need plumbing.

codecatch supports **both at the same time**, dedupes results, and exposes one
HTTP API the rest of your stack can call.

## Features (target)

- **Pluggable mail sources**: direct IMAP basic-auth (GMX, Yandex, Mail.ru, ...),
  OAuth2 (Google, Microsoft via Mozilla Thunderbird's public client_id), and
  central forwarding inboxes.
- **Pluggable extractors**: regex-driven, one per platform (TikTok, Instagram,
  Microsoft, generic fallback). Add your own via admin UI.
- **Multi-tenancy** with API-key scoping.
- **Admin web UI**: dashboard, codes browser, mailbox CRUD, providers CRUD,
  extractor playground, Plotly metrics, multi-admin.
- **REST API** with API-key auth, long-poll `/codes/wait`, pull `/codes`.
- **Headless OAuth consent** via Playwright (manual fallback when challenged).
- Password storage is encrypted but **reversible** (Fernet) with rotation
  history — pragmatic for managed-account use cases.

## Status

Pre-alpha. Active scaffolding.

## Quick start (local dev)

```bash
cp .env.example .env
# edit .env — at minimum set POSTGRES_PASSWORD, SECRET_KEY, ENCRYPTION_KEY,
# BOOTSTRAP_ADMIN_USER, BOOTSTRAP_ADMIN_PASSWORD.

docker compose up --build
# api:     http://localhost:8080/healthz
# admin:   http://localhost:8080/admin
```

Bootstrap creates one super-admin + one default tenant + one bootstrap API-key
printed once in the API logs. Save it.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  codecatch_api   FastAPI + Jinja2 + HTMX                         │
│  • REST /api/v1                                                  │
│  • Admin /admin (web UI)                                         │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  Postgres 16                                                     │
│  tenants, admins, providers, mailboxes, mailbox_passwords,       │
│  codes, api_keys, extractor_patterns, settings, audit_log         │
└─────────────────────────────────────────────────────────────────┘
                          ▲
                          │
┌─────────────────────────────────────────────────────────────────┐
│  codecatch_workers   long-running background process             │
│  • IMAP IDLE per direct mailbox + group inboxes                  │
│  • OAuth headless consent jobs (Playwright)                      │
│  • Code extraction pipeline                                      │
└─────────────────────────────────────────────────────────────────┘
```

## License

[MIT](LICENSE)
