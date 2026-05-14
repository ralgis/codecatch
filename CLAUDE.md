# CLAUDE.md — codecatch

Корневой документ контекста. Подсистемные `CLAUDE.md` (см. §3) кумулятивно
дополняют его при работе в соответствующих папках.

## 1. ЧТО ЭТО

**codecatch** — self-hosted микросервис-роутер email-кодов авторизации. Берёт
письма с verification-кодами из множества почтовых ящиков (через IMAP / OAuth
/ forwarding) и отдаёт их по HTTP API клиентам, которые регистрируют
аккаунты на сторонних платформах (TikTok, Instagram, Microsoft, Google, …).

Use-case на проекте `audiotrace-scraper`: пачки покупных hotmail/yandex/gmx-
ящиков для регистраций IG/TT-аккаунтов. Audiotrace зовёт codecatch
`POST /mailboxes` + `POST /codes/wait`, получает код, заполняет в форму.

Public repo: <https://github.com/ralgis/codecatch> · MIT.

## 2. АРХИТЕКТУРА

```
                        ┌────────────────────────────────────────────┐
                        │  codecatch                                  │
                        │                                             │
                        │  ┌────────────────┐  ┌──────────────────┐  │
HTTP clients ──REST────▶│  │ codecatch_api  │  │codecatch_workers │  │
(audiotrace,             │  │  • /api/v1     │  │  • IMAP IDLE     │  │
 admin browser)          │  │  • /admin (UI) │  │  • OAuth headless│  │
                         │  │  • /oauth/...  │  │  • OAuth refresh │  │
                         │  └────────┬───────┘  │  • Forward probe │  │
                         │           │          └────────┬─────────┘  │
                         │           ▼                   ▼            │
                         │  ┌──────────────────────────────────────┐ │
                         │  │  codecatch_postgres                  │ │
                         │  │  10 tables + LISTEN/NOTIFY           │ │
                         │  └──────────────────────────────────────┘ │
                         └────────────────────────────────────────────┘
                                          │
                                          │ IMAP / SMTP / OAuth
                                          ▼
                              GMX · Yandex · Mail.ru ·
                              Outlook · Gmail · …
```

**Три контейнера** в `docker-compose.yml`:

| Сервис | Образ | Назначение |
|---|---|---|
| `codecatch_postgres` | `postgres:16-alpine` | Stateful storage |
| `codecatch_api` | `ghcr.io/ralgis/codecatch-api` | FastAPI веб (REST + admin UI) |
| `codecatch_workers` | `ghcr.io/ralgis/codecatch-workers` | Background async loops (Playwright-base, ~700MB) |

**Поток "пришёл код":**
1. Письмо прилетает в группу-ящик (например `codes.social@gmx.de`) либо
   напрямую в зарегистрированный mailbox.
2. `imap_worker` через IMAP IDLE моментально получает уведомление и фетчит
   письмо.
3. `normalizer` парсит RFC822 → структурированный объект.
4. `extractor` прогоняет письмо через regex-паттерны из таблицы
   `extractor_patterns` (приоритет → платформа → код).
5. `code_writer` дедупит по `Message-ID`, делает `INSERT INTO codes`,
   шлёт `pg_notify('codes_tenant_<id>', code_id)`.
6. Клиент сидит на `POST /api/v1/codes/wait` — long-poll просыпается на
   pg_notify, проверяет `target_address`, отдаёт JSON с кодом.

**Routing-стратегия** (`codecatch.mailbox_service._decide_status`):
- `auto` (default): basic-auth провайдеры → direct IMAP; OAuth провайдеры →
  rely_on_groups если групповой ящик есть, иначе headless OAuth.
- `direct_only` / `group_only` / `both` — явные override'ы для случаев,
  когда нужно отказаться от автомата.

## 3. НАВИГАЦИЯ (subfolder CLAUDE.md)

| Путь | Что там | Когда читать |
|---|---|---|
| [codecatch/CLAUDE.md](codecatch/CLAUDE.md) | Shared package — config, db, crypto, auth, mailbox_service, bootstrap | Когда правишь общую логику или модели |
| [api/CLAUDE.md](api/CLAUDE.md) | FastAPI app — REST endpoints, admin UI routes, templates | Когда правишь web-уровень |
| [workers/CLAUDE.md](workers/CLAUDE.md) | Async workers — IMAP IDLE, OAuth Playwright, refresh, probe | Когда правишь background-логику |
| [db/CLAUDE.md](db/CLAUDE.md) | Schema, миграции, seed | Когда меняешь схему БД |

Templates лежат в `templates/` (Jinja2 + Bootstrap 5 + HTMX), extractors —
в `extractors/` (пока пустой, regex-патерны живут в БД).

## 4. STACK

- **Python 3.12** (slim для api, MS Playwright image для workers)
- **FastAPI 0.119** + **uvicorn** + **Jinja2** + HTMX + Bootstrap 5
- **asyncpg** — async Postgres driver, без ORM (raw SQL)
- **PostgreSQL 16** с LISTEN/NOTIFY для long-poll
- **Cryptography (Fernet)** — обратимое шифрование паролей/токенов
- **bcrypt** — admin password hashing
- **imap-tools** (поверх стандартного `imaplib`) — IMAP с XOAUTH2 support
- **patchright** — anti-detect форк Playwright (вместо vanilla playwright)
- **httpx** — async HTTP клиент для OAuth /token endpoints
- **structlog** — структурированное логирование (но рендерится для людей)

## 5. QUICK START

```bash
# Локально:
cp .env.example .env
# Сгенерируй секреты (см. .env.example для команд)
docker compose up --build -d

# Проверь:
curl http://localhost:8080/healthz
# открой http://localhost:8080/admin (логин из .env BOOTSTRAP_ADMIN_*)
```

Первый запуск **печатает в лог bootstrap API-key** — сохрани его:
```
bootstrap.api_key_created token=ccr_live_... note=SAVE THIS NOW
```

Подробный smoke-test → [docs/SMOKE_TEST.md](docs/SMOKE_TEST.md) (если файл не
существует — README в корне покрывает 90%).

## 6. КОНВЕНЦИИ КОДА

- **Python 3.12**, type-hints обязательны для публичного API.
- **PEP 8**: snake_case, max line 100.
- **f-strings** для форматирования.
- **Импорты:** stdlib → third-party → local. Грузить тяжёлые зависимости
  (playwright, patchright) **лениво внутри функций**, если код шарится
  между api и workers контейнерами.
- **No ORM.** asyncpg + raw SQL. Параметры через `$1, $2, ...`.
- **Async везде.** API хэндлеры, DB-запросы, worker'ы — всё `async def`.
  Блокирующие libs (imap-tools синхронен) оборачиваем `asyncio.to_thread`.
- **structlog** для логов, `log.info("event_name", key=value)`. Не Python
  `logging` напрямую.
- **Errors** в API возвращаются как `{"error": "<code>", "message": "..."}`
  с фиксированным форматом (см. `api/schemas.py::ErrorResponse`).
- **Комментарии:** только WHY, не WHAT. Не ссылаемся на коммиты / задачи —
  PR-описание для этого.

## 7. CRYPTO STRATEGY

Три разных подхода для трёх категорий секретов:

| Что | Чем | Почему |
|---|---|---|
| Mailbox passwords + OAuth refresh-tokens | **Fernet** (AES-128-CBC + HMAC) | Нужно расшифровывать для IMAP/OAuth — обратимое |
| Admin passwords | **bcrypt** | One-way, нужно только сравнивать на login |
| API-key tokens | **SHA-256** | High-entropy, сравнение через `hmac.compare_digest` |

Master Fernet-ключ — `ENCRYPTION_KEY` в `.env`. **Потеря = потеря всех
сохранённых паролей**, бэкап ключа критичен (написать в Bitwarden /
1Password при создании). Cм. `codecatch/crypto.py`.

## 8. MULTI-TENANCY MODEL

Все таблицы кроме `providers`, `extractor_patterns`, `settings`, `audit_log`
имеют `tenant_id`. **Каждый запрос фильтруется по нему**:
- Admin session → `admins.tenant_id` (NULL для super-admin)
- API key → `api_keys.tenant_id`

Группы-ящики (`mailboxes.is_group=TRUE`) **scoped to tenant** — codes
приходят в свою tenant'у автоматически через `target_address` lookup в
mailboxes-таблице. Если target не найден в текущем tenant'е, codes
сохраняются под tenant'ом источника (forwarding ящика).

Bootstrap создаёт один tenant `slug='default'` + один super-admin + один
admin-scope API-key (`name='bootstrap'`). Дальнейшее управление tenant'ами
— через `/admin/tenants` (только super-admin).

## 9. МИГРАЦИИ БД — ПРАВИЛА

**Два разных места для SQL:**

- **`db/init/00_schema.sql` + `01_seed.sql`** — выполняются **только** при
  первом запуске пустого postgres-volume (через
  `/docker-entrypoint-initdb.d/`). На существующих БД не применяются.
- **`db/migrations/NNNN_*.sql`** — выполняются `codecatch.migrations` при
  каждом старте api контейнера, после `schema_migrations` (трекинг применённых
  по `version`).

**Правило:** новые изменения схемы — миграцией. Параллельно обновляй
`00_schema.sql` чтобы fresh-инсталляция получала всё сразу.

Идемпотентность: все миграции пишутся с `IF NOT EXISTS` / `ON CONFLICT DO
NOTHING` / `ADD COLUMN IF NOT EXISTS`. Запуск дважды не должен ломать.

Подробнее → [db/CLAUDE.md](db/CLAUDE.md).

## 10. ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ И WORKAROUND'Ы

### 10.1 OAuth Microsoft / Hotmail

Используем публичный `client_id` Mozilla Thunderbird (
`9e5f94bc-e8a4-4e73-b8be-63364c29d753`). У него **зарегистрирован только
`nativeclient` redirect URI** — для browser-based manual flow это даёт
страницу "phishing warning" с auto-redirect, и юзер не успевает скопировать
auth-code из URL.

**Принятое решение:** для Hotmail используем **forwarding на группу-ящик**
(см. `rely_on_groups` статус). Direct OAuth для Hotmail работает только
через свой Azure-app — отложен.

### 10.2 OAuth Google / Gmail

Mozilla'й `client_id` имеет `http://127.0.0.1:PORT` зарегистрированный
(loopback per RFC 8252) — теоретически manual flow работает без своего
Cloud-project. Не проверено end-to-end (см. previous chat). Когда нужен —
выставить `REDIRECT_URI_GOOGLE = "http://localhost:8080/oauth/callback"` в
`workers/oauth_worker.py` + `api/routes/oauth.py`.

### 10.3 GMX IMAP IDLE early-EOF

GMX рубит IDLE-соединение каждые ~10-15 мин (не RFC-стандартные 29 мин).
Worker автоматически переподключается с exponential backoff. Между EOF и
reconnect (5-30 сек) real-time push не работает, но `last_seen_uid` +
backlog-fetch + dedup по `Message-ID` страхуют. Real fix: periodic NOOP /
re-IDLE every 5 min.

### 10.4 Microsoft anti-bot challenge

При логине с неизвестного IP MS показывает proof-confirmation challenge
(введи recovery email → получи код на recovery → введи код). Без
residential IP headless OAuth для Microsoft не пройдёт. Patchright решает
bot-detection (форма рендерится), но не account-verification.

## 11. ОБРАБОТКА ОШИБОК

- **structlog `log.exception(...)`** для unexpected exceptions с tracebacks.
- **Никогда `except Exception: pass`** — если глотаем, логируем причину.
- **Worker loops** — оборачиваем тики в `try/except`, при ошибке логируем
  и спим backoff'ом. Не давать одной ошибке остановить весь worker.
- **API endpoints** — поднимаем `HTTPException(detail={"error": "...",
  "message": "..."})` с правильным HTTP-кодом. См. полный список в
  `README.md` секции "Errors".
- **Worker side-effects при ошибках:** статус mailbox'а обновляется
  отражает причину (`oauth_last_error`, `last_error`,
  `forwarding_probe_error` и т.п.). Никаких retry-bombs без backoff'а.

## 12. БЕЗОПАСНОСТЬ

- **`.env` никогда не коммитим** (в `.gitignore`). Только `.env.example`.
- **Master Fernet-key** в `.env` — бэкап в password manager обязателен.
- **Admin session-cookie** подписан itsdangerous через `SECRET_KEY`,
  `httponly`, `samesite=lax`. В prod ставить `secure=True` (см. login.py).
- **API keys** в БД только SHA-256 хэш + первые 12 символов префикса для
  display. Plain-text token показывается **один раз** при создании.
- **Audit log** на чувствительные операции: admin.login,
  mailbox.password.reveal, api_key.create / revoke и т.д.
- **Repo публичный** (MIT). Никакие credentials, никакие тестовые токены,
  никакие customer email-адреса не должны попадать в commit'ы.

## 13. CI/CD

`.github/workflows/build.yml` запускается на push в main:
1. **test** job: `ruff check .` + `pytest -q` (smoke).
2. **build-api** + **build-workers** jobs параллельно: собирают образы и
   пушат в `ghcr.io/ralgis/codecatch-api:latest` + `:<sha>` и
   `ghcr.io/ralgis/codecatch-workers:latest` + `:<sha>`. Public GHCR
   (потому что repo public — unlimited Actions minutes).

Деплой пока не автоматизирован: на VPS делается `git pull` + `docker
compose pull` + `docker compose up -d`.

## 14. ОТЛАДКА (cheat sheet)

```bash
# Логи
docker compose logs -f api
docker compose logs -f workers | grep -E "code_writer|imap_worker"

# DB shell
docker compose exec postgres psql -U codecatch -d codecatch

# Что миграции применились
docker compose exec postgres psql -U codecatch -d codecatch \
  -c "SELECT version, applied_at FROM schema_migrations ORDER BY version"

# Список провайдеров
docker compose exec postgres psql -U codecatch -d codecatch \
  -c "SELECT name, auth_kind, oauth_strategy FROM providers ORDER BY name"

# Сбросить mailbox для retry OAuth flow
docker compose exec postgres psql -U codecatch -d codecatch -c \
  "UPDATE mailboxes SET status='pending_oauth_headless', oauth_consent_url=NULL,
   oauth_last_error=NULL, headless_attempt_count=0 WHERE address='X@hotmail.com'"

# Force IMAP refetch backlog
docker compose exec postgres psql -U codecatch -d codecatch -c \
  "DELETE FROM settings WHERE key = 'imap.last_seen_uid.X@gmx.de'"

# Декодировать pg_notify канал
docker compose exec postgres psql -U codecatch -d codecatch -c "LISTEN codes_tenant_1"

# Скриншоты + HTML провалов OAuth headless
ls debug/<address>/    # mounted volume from codecatch_workers
```

## 15. ТЕСТ-ДАННЫЕ В DEV

В git-историю не коммитим, но в локальной БД на разработческой машине:
- `codes.social@gmx.de` — group inbox, basic auth, `direct_active`
- `figelbrite4l@hotmail.com`, `lenanishi9363@hotmail.com` — hotmail'ы,
  forwarding'ом доставляют коды в codes.social
- bootstrap admin / `admin` / пароль из BOOTSTRAP_ADMIN_PASSWORD

Учитывай: `.env` локальный — для prod **новые** секреты сгенерировать.
