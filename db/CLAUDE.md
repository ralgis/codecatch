# CLAUDE.md — `db/` (schema + migrations + seed)

Корневой контекст — в [../CLAUDE.md](../CLAUDE.md).

## ДВА МЕСТА ДЛЯ SQL

```
db/
├── init/                          ── ВЫПОЛНЯЕТСЯ ОДИН РАЗ, на пустой volume
│   ├── 00_schema.sql              ── полная схема + индексы + constraints
│   └── 01_seed.sql                ── pre-seeded providers + extractor patterns
└── migrations/                    ── ВЫПОЛНЯЕТСЯ КАЖДЫЙ СТАРТ api контейнера
    ├── 0002_mode_and_forwarding.sql
    └── ...                        ── numbered SQL files, idempotent
```

### `db/init/*` — bootstrap layer

Mounted в `codecatch_postgres` контейнер как
`/docker-entrypoint-initdb.d/`. Стандартный postgres entrypoint
**автоматически запускает** все `.sql` / `.sh` файлы оттуда в
алфавитном порядке — **но только если data directory пустой** (т.е.
volume чистый).

**Применяется:**
- Первый docker compose up на пустом volume
- После `docker volume rm codecatch_codecatch_pgdata`

**Не применяется:**
- Каждый последующий `docker compose up`
- При обновлении контейнера (если pgdata цел)

То есть `00_schema.sql` — это **snapshot текущей схемы для fresh
install**. Когда добавляем колонки/таблицы — обновляем здесь же ТАКЖЕ.

### `db/migrations/*` — для существующих deployment'ов

Runner: `codecatch/migrations.py::run_migrations()`. Вызывается в
lifespan API контейнера сразу после создания pool, до bootstrap.

Файлы:
- Имя: `NNNN_short_description.sql` (4-значный номер для лексической
  сортировки)
- Применяются один раз — tracked через `schema_migrations.version`
- **Должны быть идемпотентны** (на случай ручного повторного применения):
  `ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`, `ON
  CONFLICT DO NOTHING`, ...

**Применяется:**
- При каждом старте api контейнера
- Только pending (которых нет в `schema_migrations`)

### Правило: добавил колонку — обнови ОБА файла

При новой schema change:

1. Создай миграцию `db/migrations/NNNN_what.sql`:
   ```sql
   ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS new_col TEXT;
   ```
2. **Также** добавь колонку в `db/init/00_schema.sql` в соответствующее
   место. Чтобы fresh install получил её сразу.

Если забудешь второе — fresh install будет работать (миграция применится
поверх init), но stale: у тебя в проекте две версии схемы. Лучше синк.

## ТАБЛИЦЫ — QUICK REFERENCE

### Управление доступом
| Таблица | Назначение |
|---|---|
| `tenants` | Логический владелец данных. `slug='default'` создаётся bootstrap'ом. |
| `admins` | Web UI users. `is_super_admin=TRUE` (tenant_id=NULL) или tenant-scoped. |
| `api_keys` | Bearer tokens для REST API. SHA-256 hash + 16-char prefix. |
| `audit_log` | Чувствительные операции — admin login, password reveal, key create/revoke. |

### Email infrastructure
| Таблица | Назначение |
|---|---|
| `providers` | Catalogue провайдеров: IMAP/SMTP host, auth_kind, OAuth client_id. Pre-seeded 11 штук. |
| `mailboxes` | Один email = одна row. Содержит status, mode, provider_id, OAuth refresh_token (encrypted), forwarding_target, и т.д. |
| `mailbox_passwords` | История паролей. Exactly один с `is_current=TRUE`. Encrypted (Fernet). |

### Codes pipeline
| Таблица | Назначение |
|---|---|
| `extractor_patterns` | Regex-патерны для извлечения кодов из писем. Per-platform приоритет. Builtin + user-added. |
| `codes` | Final output — извлечённые коды. Dedup'ятся по `(target_address, message_id)`. |
| `settings` | Key-value singleton store. Используется для `imap.last_seen_uid.*`, `oauth.flow.<state>` и пр. |

## КЛЮЧЕВЫЕ ИНДЕКСЫ

- `codes(tenant_id, target_address, received_at DESC)` — для `/codes/wait`
  и `/codes` pull endpoint
- `codes(target_address, message_id) WHERE message_id IS NOT NULL UNIQUE`
  — dedup
- `mailbox_passwords(mailbox_address) WHERE is_current = TRUE UNIQUE` —
  enforce "только один current"
- `mailboxes(is_group, is_active) WHERE is_group AND is_active` — для
  быстрого выбора активных групп
- `mailboxes(status, created_at) WHERE status IN ('pending_oauth_*')` —
  для OAuth queue
- `providers USING GIN (domain_patterns)` — для domain lookup'а

## CONSTRAINTS КОТОРЫХ СТОИТ ЗНАТЬ

```sql
-- admin scope: super-admin без tenant, tenant-admin с tenant
CHECK (
    (is_super_admin = TRUE AND tenant_id IS NULL)
    OR
    (is_super_admin = FALSE AND tenant_id IS NOT NULL)
)

-- mailbox.mode: только 4 значения
CHECK (mode IN ('auto', 'direct_only', 'group_only', 'both'))

-- providers.auth_kind
CHECK (auth_kind IN ('basic', 'oauth_google', 'oauth_microsoft', 'oauth_generic'))
```

Добавляешь новое значение — обнови **и** CHECK constraint **и** код
который маппит status'ы (`api/templating.py::humanize_status`,
`workers/imap_worker.reconcile_workers` WHERE clause).

## PG_NOTIFY КАНАЛЫ

**Convention:** один канал на tenant — `codes_tenant_<id>`. Payload —
JSON `{code_id, target_address}`.

Кто публикует: `workers/code_writer.py::process_and_store()` после успешного
INSERT.

Кто слушает: `api/routes/api_v1.py::code_wait()` через
`conn.add_listener(channel, ...)` на отдельном acquired connection.

## SEED DATA

`db/init/01_seed.sql` содержит:

### 11 providers
- **Basic auth (8)**: GMX (DE), GMX (com), Yandex, Mail.ru, Yahoo, Web.de,
  T-Online, iCloud, Migadu
- **OAuth (2)**: Google, Microsoft — с Mozilla Thunderbird client_id

### 8 extractor patterns
- TikTok (subject 6-digit)
- Instagram (body / subject 4-8 digit)
- Microsoft (security/verification/one-time + RU "Код безопасности /
  Разовый код")
- Twitter/X
- Facebook
- 2× generic fallback (subject 4-8 digit + body "code/pin/otp")

При добавлении нового провайдера или extractor'а — обнови `01_seed.sql`.
**Не** инициализируй через миграцию (миграции — для существующих БД,
provider'ы вставлены при init).

Если провайдер должен появиться **во всех существующих deployment'ах**
без resetting volume — нужна и миграция, и обновление init. Например:

```sql
-- 0003_add_proton_provider.sql
INSERT INTO providers (name, domain_patterns, ...) VALUES (...)
ON CONFLICT (name) DO NOTHING;
```

## ТИПЫ ПОЛЕЙ — RATIONALE

- `TEXT` всегда вместо `VARCHAR(N)` — Postgres хранит их одинаково,
  TEXT короче и без артбитрарного лимита
- `TIMESTAMPTZ` для всех временных меток — never naive
- `JSONB` для `settings.value`, `extractor_patterns.code_pattern` —
  гибкий, индексируемый
- `TEXT[]` для `providers.domain_patterns`, `providers.oauth_scopes`,
  `audit_log.metadata` — массивы Postgres надёжны, GIN-индексируемы
- `BIGSERIAL` для всех auto-PK — IDs за int32 не выберут, но дёшево

## БАЗОВЫЕ ОПЕРАЦИИ ДЛЯ ДЕБАГА

```sql
-- Что у нас в очереди OAuth
SELECT address, status, headless_attempt_count, oauth_last_error
FROM mailboxes
WHERE status LIKE 'pending_oauth%'
ORDER BY created_at;

-- Стрим pg_notify в реальном времени
LISTEN codes_tenant_1;
-- (отдельный psql session, потом ждать)

-- Последние 10 кодов
SELECT id, target_address, code, platform, received_at, consumed_at
FROM codes ORDER BY received_at DESC LIMIT 10;

-- Сила deduplication
SELECT target_address, message_id, COUNT(*) FROM codes
GROUP BY target_address, message_id HAVING COUNT(*) > 1;
-- (должен быть пустым)

-- Какой UID последний фетчили
SELECT key, value FROM settings WHERE key LIKE 'imap.last_seen_uid.%';
```

## BACKUP / RESTORE

Для production:
```bash
docker compose exec postgres pg_dump -U codecatch codecatch > backup.sql
```

Critical: **бэкап `ENCRYPTION_KEY`** отдельно. Без неё все mailbox
passwords и OAuth refresh_tokens — мусор.
