# CLAUDE.md — `codecatch/` shared package

Корневой контекст — в [../CLAUDE.md](../CLAUDE.md).

Этот пакет содержит **общий код**, импортируемый и `api/`, и `workers/`.
Поэтому **никаких тяжёлых зависимостей** на module-level (Playwright,
imap-tools — нет). Только то что нужно обеим сторонам:

| Модуль | Что делает |
|---|---|
| `config.py` | pydantic-settings — `.env` → typed `Settings`. Singleton через `lru_cache`. |
| `db.py` | Тонкие helper'ы поверх asyncpg.Pool. Не ORM. |
| `crypto.py` | Fernet encrypt/decrypt, bcrypt hash/verify, API-key generate/hash. |
| `logging_setup.py` | structlog configure + get_logger. |
| `bootstrap.py` | First-run setup: default tenant + super-admin + bootstrap API-key. Идемпотентно. |
| `auth.py` | FastAPI dependencies + session-cookie/API-key resolution. Знает только asyncpg, не зависит от templating и т.д. |
| `audit.py` | Single entry-point для записей в `audit_log`. |
| `providers.py` | Lookup providers по домену email'а. |
| `mailbox_service.py` | Strategy selector — упомянутая в root CLAUDE.md "сердцевина routing'а". |
| `migrations.py` | In-process runner для `db/migrations/*.sql`. Запускается на старте api. |

## КЛЮЧЕВЫЕ ПРИНЦИПЫ

### Никаких глобальных зависимостей кроме pool

Все функции принимают `asyncpg.Pool` явным аргументом. Никаких глобальных
переменных типа `db = None`. Это упрощает тесты и позволяет нескольким
пулам жить параллельно (например, separate read-only pool).

### Encryption-функции — только модуль `crypto.py`

Если видишь Fernet/`encrypt()`/`decrypt()` вне `codecatch/crypto.py` —
это плохо. Все ciphertext ↔ plaintext перевыводы должны идти через этот
модуль. Так если когда-нибудь будем ротировать `ENCRYPTION_KEY`, нужно
поменять одно место + миграция.

### Pydantic-settings — единственный entry-point для env

Не делай `os.environ["XXX"]` в коде. Добавь поле в `Settings`, тогда:
- Появится в `.env.example` (когда вспомним обновить — добавь!)
- Будет валидация типов на старте
- IDE-autocomplete

### `mailbox_service.upsert_mailbox` — единственная точка изменения mailboxes

Не используй прямой `INSERT/UPDATE mailboxes` из других мест. Логика:
- проверка существования
- preservation tenant_id
- сравнение и ротация password (с историей)
- провайдер resolution
- strategy decision → status

— **всё в одной транзакции**. Дублировать это в admin route'ах или REST
endpoint'ах == баг. Если нужна новая операция над mailbox'ами — добавь
функцию в `mailbox_service.py`.

### `_decide_status` — pure function

Не делает писем в БД, не запускает Playwright. Просто читает текущее
состояние и возвращает `(status, note)`. Все side-effects (worker pickup,
IMAP login, и т.п.) запускаются ДРУГИМИ компонентами на основе нового
status'а.

## DEPENDENCY GRAPH ВНУТРИ ПАКЕТА

```
config ──► (все остальные)
db ◄────── (все остальные)
crypto ◄── auth, bootstrap, mailbox_service
logging_setup ◄── (все остальные)
bootstrap ──► config, crypto, logging_setup
auth ──► config, crypto
audit ──► (никого)
providers ──► (никого, только asyncpg)
mailbox_service ──► crypto, providers, logging_setup
migrations ──► logging_setup
```

Не вводить циклы! Если кажется что нужен — посмотри что можно вытащить в
helper-функцию вне пакета.

## ЕСЛИ ПРАВИШЬ FERNET

`crypto.py::_fernet()` каждый вызов делает `Fernet(key.encode())`. На
hot-path (тысячи decrypt'ов в секунду) это дорого. Если профайл покажет
проблему — кэшируем через `lru_cache`. Сейчас не нужно.

## ЕСЛИ ПРАВИШЬ AUTH

`auth.py` exposes:
- `get_current_admin_optional` → `CurrentAdmin | None` (для login-page)
- `require_admin` → 303 redirect to /login если нет сессии
- `require_super_admin` → 403 если admin не super
- `require_api_key` → 401 если нет ключа
- `require_admin_scope_key` → 403 если ключ не admin-scope

В route'ах используем через `Annotated[CurrentX, Depends(require_X)]`.
**Не** через global state, не через middleware — FastAPI deps это
идиоматический способ.

## ЕСЛИ ПРАВИШЬ BOOTSTRAP

`run_bootstrap()` идемпотентно — может вызываться многократно, ничего не
дублирует. **Печатает clear-text API-key в логи ровно один раз** при
первой инициализации tenant'а. Никогда не сохраняет его в plain-text в БД.
Если bootstrap-key потерян — оператор создаёт новый через admin UI.

## ЕСЛИ ПРАВИШЬ MAILBOX_SERVICE

Strategy decision tree (`_decide_status`) сложный, осторожно. Все возможные
mailbox.status значения:
- `pending` — только что INSERT'нули, ещё не resolved
- `direct_active` — basic IMAP login будет happen
- `rely_on_groups` — будем читать через group inbox
- `pending_oauth_headless` — worker подхватит Playwright OAuth
- `pending_oauth_manual` — headless провалился, ждём human consent
- `oauth_active` — refresh_token есть, IMAP via XOAUTH2 готов
- `invalid_credentials` — login отвергнут
- `unknown_provider` — домен не в `providers`
- `no_path` — ни один путь не доступен (нет group, нет creds, нет OAuth)

Добавляешь новый статус? Обнови:
1. `_decide_status` (логика выдачи)
2. SQL CHECK constraint в `mailboxes.status` (если ввели)
3. `imap_worker.reconcile_workers` (если worker должен подхватывать)
4. `api/templating.py::humanize_status` (для UI)

## ТЕСТЫ

Сейчас тесты в `tests/test_smoke.py` — только импорт-проверки. Когда будем
писать настоящие — целиться в `codecatch/mailbox_service` (strategy decision)
и `codecatch/crypto` как самые tricky.
