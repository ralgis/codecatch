# CLAUDE.md — `api/` (FastAPI app)

Корневой контекст — в [../CLAUDE.md](../CLAUDE.md).

`api/` — это **web layer**. Содержит:
- FastAPI app entrypoint (`main.py`)
- HTTP routes — три плоскости: REST API, admin web UI, OAuth callback
- Pydantic schemas для REST API request/response
- Jinja2 templating helpers

## РАЗДЕЛЕНИЕ "REST API vs ADMIN UI"

Это **разные surfaces** для разных аудиторий:

| | REST `/api/v1/*` | Admin `/admin/*` |
|---|---|---|
| Кто пользуется | machine (audiotrace, скрипты) | оператор-человек в браузере |
| Auth | `Authorization: Bearer ccr_live_...` (API-key, SHA-256 hash в БД) | session cookie (bcrypt password + signed cookie) |
| Response | JSON, фиксированный error format | HTML (Jinja2), redirect'ы 303 |
| Multi-tenancy | tenant из API-key | tenant из admin (NULL для super-admin) |
| Файл с роутами | `api/routes/api_v1.py` | `api/routes/admin.py` + `login.py` |

OAuth-callback (`/oauth/callback`) — третий surface, **без auth**, защищён
через `state` параметр (одноразовый, связан с конкретным flow'ом). См.
`api/routes/oauth.py`.

## СТРУКТУРА ФАЙЛОВ

```
api/
├── __init__.py
├── main.py           # FastAPI app + lifespan + healthz + root + router includes
├── schemas.py        # Pydantic request/response для REST API
├── templating.py     # Jinja2 environment + custom filters
└── routes/
    ├── __init__.py
    ├── login.py      # /login, /logout — session cookie issuance
    ├── admin.py      # /admin/* — все web pages для оператора
    ├── api_v1.py     # /api/v1/* — REST для машин
    └── oauth.py      # /oauth/callback + /admin/oauth-paste
```

## ВАЖНЫЕ ПРАВИЛА FASTAPI

### Route order matters

FastAPI матчит routes **в порядке регистрации**. Если у тебя:

```python
@router.get("/mailboxes/{address}")      # → matches anything
@router.get("/mailboxes/pending-consent") # → never reached!
```

— литеральные пути **обязательно** регистрировать **до** dynamic (`{x}`).
Мы на этом уже один раз обожглись — см. git log `f84e1cb` and follow-up.

### `Annotated` vs default values

Pydantic v2 + FastAPI: внутри `Annotated[]` **нельзя** ставить default
через первый аргумент `Form(...)` / `Query(...)`. Только `Form()` / `Query()`
без default, а default — через `=`:

```python
# ✗ ОШИБКА — AssertionError на старте
password: Annotated[str, Form("")] = ""

# ✓ ОК
password: Annotated[str, Form()] = ""

# ✓ ОК (без default — required)
password: Annotated[str, Form()]
```

### Request как параметр

Если нужен `Request` объект — **явно типизировать** `request: Request`,
иначе FastAPI решит что это query-параметр и потребует его. Мы это уже
ловили на `/healthz`.

### Lazy imports для Playwright

`workers/forwarding_setup.py` (и потенциально другие места) импортируют
Playwright. Но `api/routes/admin.py` его тоже импортирует transitively
(через ту же функцию). API контейнер **не имеет** Playwright. Решение:
ленивый импорт внутри функции — модуль загружается, функция вызывается
только когда нужна, и если pw нет — возвращает ошибку.

## АДМИН-UI: STYLE

- **Bootstrap 5** через CDN (никакого webpack)
- **HTMX** через CDN для динамики (auto-refresh таблиц, badge update)
- **Bootstrap Icons** для иконок
- Темплейты живут в `templates/`, базовый `base.html` со сайдбаром.
- Цветовая палитра: `data-bs-theme="dark"` в `<html>` (тёмная тема
  принудительно). Badge'и: `text-bg-success / warning / danger / info`.

Не вводить отдельный CSS/JS bundle. Если нужна логика на странице — inline
`<script>` в шаблоне. Mantra: **server-rendered first**, JS только для
оживления (auto-refresh, copy-to-clipboard, etc.).

## REST API: ERROR FORMAT

Все ошибки одинакового шейпа:

```json
{
  "detail": {
    "error": "machine_readable_code",
    "message": "Human readable"
  }
}
```

HTTP codes:
- 200/201/204 — success
- 400 `validation_error` / `unknown_provider` / `invalid_credentials`
- 401 `unauthorized` — нет/invalid API-key
- 403 `forbidden` — wrong scope
- 404 `not_found`
- 409 `conflict`
- 410 `gone` — code already consumed
- 429 `rate_limited` (зарезервировано, ещё не реализовано)

Старайся НЕ возвращать unwrapped FastAPI default `{"detail": "string"}`.
Используй `HTTPException(detail={"error": "...", "message": "..."})`.

## LONG-POLL `/codes/wait`

Endpoint держит соединение до 300 секунд, ждёт `pg_notify('codes_tenant_X',
...)`. Внутри:

1. Сразу `SELECT` — может быть код уже есть в БД.
2. `LISTEN codes_tenant_X` через отдельный pool acquire.
3. `asyncio.wait_for(notify_event.wait(), timeout=min(remaining, 5))`.
4. На notify — повторяем SELECT.

Каждая waiting connection держит **один asyncpg connection из пула**. При
`max_size=10` параллельных waits может быть до 10. В prod scale —
увеличить pool size или вынести wait-handling в отдельный read-pool.

## ADMIN ROUTES — TENANT SCOPING

В каждом query фильтр по tenant_id:
- Super-admin (admin.is_super_admin=True) → видит всё
- Tenant-admin → только свой tenant_id

Helper-функция `_tenant_filter` + `_q` в `admin.py` строит динамический
WHERE-clause. Не самое элегантное решение — но избегает накладывания
ORM сверху.

При добавлении новой admin-страницы:
1. **Всегда** `Depends(require_admin)` — без auth не пустить
2. **Всегда** проверь tenant scope в SELECT/UPDATE
3. Для super-only страниц → `Depends(require_super_admin)`

## ШАБЛОНЫ (TEMPLATES)

Жонглируем тремя контекстами:
- `request` (FastAPI авто-passes) — для url'ов и пр.
- `admin` (CurrentAdmin) — текущий юзер (для conditional UI)
- `...` (page-specific data)

**Auto-refresh таблиц** через HTMX:
```html
<span hx-get="/admin/codes" hx-trigger="every 10s"
      hx-select="tbody" hx-target="tbody" hx-swap="outerHTML"></span>
```

## OAUTH /CALLBACK

Шарит логику с paste-back через `_do_exchange`. Если меняешь Google
provider implementation — тронь в **двух местах**:
- `workers/oauth_worker.py::_exchange_code_for_tokens` (headless flow)
- `api/routes/oauth.py::_do_exchange` (manual callback flow)

Дублирование оправдано тем что workers контейнер тяжёлый (с Playwright),
api лёгкий (без). Не хотим тащить друг в друга всё.

## ROUTE TEMPLATES

При добавлении нового admin endpoint:

```python
@router.get("/somepage", response_class=HTMLResponse)
async def somepage(
    request: Request,
    admin: Annotated[CurrentAdmin, Depends(require_admin)],
):
    pool = request.app.state.db_pool
    where = ["TRUE"]
    args: list[Any] = []
    if not admin.is_super_admin:
        args.append(admin.tenant_id)
        where.append(f"tenant_id = ${len(args)}")
    rows = await pool.fetch(
        f"SELECT ... WHERE {' AND '.join(where)} ORDER BY ...",
        *args,
    )
    return templates.TemplateResponse(
        request, "somepage.html", {"admin": admin, "rows": rows}
    )
```

При добавлении нового REST endpoint:

```python
@router.get("/something", response_model=SomethingResponse)
async def something(
    request: Request,
    key: Annotated[CurrentApiKey, Depends(require_api_key)],
):
    pool: asyncpg.Pool = request.app.state.db_pool
    row = await pool.fetchrow(
        "SELECT ... WHERE tenant_id = $1 ...",
        key.tenant_id,
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "..."},
        )
    return SomethingResponse(...)
```

## ТЕСТЫ

Когда добавим — `httpx.AsyncClient` + fixture'а с pool на test-DB.
Сейчас только smoke-imports.
