# CLAUDE.md — `workers/` (background async loops)

Корневой контекст — в [../CLAUDE.md](../CLAUDE.md).

`workers/` — это **долгоживущий процесс** в отдельном контейнере с
Playwright/Chromium. Запускается через `python -m workers.main`. Внутри
параллельно крутятся **четыре независимых loops** + утилиты:

| Модуль | Loop / utility | Что делает |
|---|---|---|
| `main.py` | entrypoint | Стартует все worker'ы как asyncio tasks, ловит SIGTERM |
| `imap_worker.py` | `ImapWorkerManager` | Per-mailbox IMAP IDLE с reconnect/backoff |
| `oauth_worker.py` | `OAuthWorker` | Очередь headless OAuth-консентов через Playwright |
| `oauth_refresh.py` | `OAuthRefresher` | Периодический refresh access_token'ов |
| `forwarding_probe.py` | `ForwardingProbeWorker` | Active test email + delivery check |
| `forwarding_setup.py` | utility (не loop) | One-shot Playwright настройка forwarding в outlook.live.com |
| `normalizer.py` | utility | RFC822 → `NormalizedMessage` dataclass |
| `extractor.py` | utility | Pure regex code extraction по patterns из БД |
| `code_writer.py` | utility | Извлечь код → INSERT в `codes` + pg_notify |

## ОБЩИЕ ПРИНЦИПЫ

### Asyncio everywhere

Все loops — `async def run(self)`. Все DB-операции — `await
pool.fetch/execute(...)`. Блокирующие library (imap-tools, smtplib) —
обёрнуты `asyncio.to_thread(...)`. Не ставим `time.sleep` — `await
asyncio.sleep`.

### Graceful shutdown

`main.py` слушает SIGTERM/SIGINT, выставляет `asyncio.Event`. Каждый
worker должен **проверять этот event** в своих loops:

```python
while not self._shutdown.is_set():
    await self._tick()
    try:
        await asyncio.wait_for(self._shutdown.wait(), timeout=N)
    except asyncio.TimeoutError:
        pass
```

При SIGTERM Docker даёт 10 секунд до SIGKILL. Чистый shutdown за это
время = браузеры закрыты, соединения закрыты, ничего не висит.

### Error containment

Один проваленный tick не должен останавливать loop. Шаблон:

```python
while not self._shutdown.is_set():
    try:
        await self._tick()
    except Exception as e:  # noqa: BLE001
        log.exception("worker.tick_failed", error=str(e))
    await asyncio.wait_for(self._shutdown.wait(), timeout=N)
```

Сам `_tick` тоже может ловить ошибки на под-операциях и продолжать
обрабатывать остальные mailbox'ы.

### Resource pooling

`pool = asyncpg.Pool` шарится **всеми** workers через один общий объект,
переданный в `main.py`. Pool `max_size=15` достаточно для 4 workers x
~3 connections каждый.

## IMAP IDLE WORKER

### Архитектура

`ImapWorkerManager.run()` каждые 15 сек делает `reconcile_workers()`:
- SELECT всех active mailboxes которые должны IDLE'ить (`status='direct_active'`
  для basic-auth + `status='oauth_active'` для OAuth)
- Сравнивает с current `self._workers` dict
- Spawn новые per-mailbox tasks для добавленных
- Cancel задачи для убранных

Каждая per-mailbox task (`_run_one`) — это retry-loop с exponential
backoff (5s → 10s → ... → 5min):

```
while not shutdown:
    try:
        await _session_loop()    # один полный IMAP цикл
        backoff = MIN
    except CancelledError:
        raise                     # respect cancellation
    except Exception:
        log + record_error
        await sleep(backoff)
        backoff = min(backoff * 2, MAX)
```

### `_session_loop()` — один full cycle

1. Open IMAP4_SSL + login (basic LOGIN или xoauth2)
2. Fetch backlog: messages with UID > last_seen_uid (или newest 20 на
   первом запуске)
3. IDLE loop: `box.idle.wait(timeout=29*60)` — если получили notification
   или timeout, перефетчим backlog
4. На сетевой ошибке — выходим из `_session_loop`, retry-loop поднимет
   новый

### Quirks

**GMX рубит IDLE через ~10-15 мин** (не RFC 29min). Worker корректно
переподключается, но есть окно 5-30 сек без real-time push. См. секцию
"Известные ограничения" в root CLAUDE.md.

**Yandex иногда требует "ID" command перед LOGIN** (Yandex-specific
протокольное расширение). Сейчас не реализовано, может вылезти на yandex
ящиках. Если увидишь "LOGIN failed" на yandex с правильным паролем —
добавь `box.id({...})` перед `box.login()`.

### last_seen_uid persistence

После обработки каждого письма пишем `settings.imap.last_seen_uid.<addr>`
= UID. На reconnect берём оттуда. При первом старте — limit 20 newest
чтобы не зафетчить inbox целиком.

## OAUTH HEADLESS WORKER

### Архитектура

Очередь — таблица `mailboxes WHERE status='pending_oauth_headless'`.
Worker берёт по одному (atomic UPDATE … FOR UPDATE SKIP LOCKED), пытается
provision OAuth через Playwright.

### Patchright не Playwright

Используем **patchright** — anti-detect форк. Импорт:

```python
try:
    from patchright.async_api import async_playwright
except ImportError:
    from playwright.async_api import async_playwright  # fallback
```

Patchright stripped:
- `navigator.webdriver`
- HeadlessChrome в User-Agent
- Различные fingerprint-ловушки

В Dockerfile.workers ставим **обе**: `playwright` (база) + `patchright`
(сверху). `patchright install chromium` качает патченный binary.

### Persistent context per mailbox

```python
context = await pw.chromium.launch_persistent_context(
    user_data_dir=f"/app/playwright_profiles/{safe_address}",
    headless=True,
    user_agent=REALISTIC_UA,
    viewport={"width": 1920, "height": 1080},
    locale="en-US",
    timezone_id="Europe/Berlin",
    color_scheme="light",
    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
)
```

Cookies / IndexedDB / localStorage сохраняются между attempts → MS
видит "тот же device что вчера" вместо fresh headless каждый раз.

Volume `codecatch_pw_profiles` mounted в compose чтобы профили
переживали container restart.

### Fallback на manual

Любая ошибка во время headless → `fell_through_to_manual`:
- Сохраняется `oauth_consent_url` в mailbox row
- Состояние OAuth-flow (state → address mapping) сохраняется в `settings`
  table с ключом `oauth.flow.<state>`
- Status → `pending_oauth_manual`
- Operator открывает URL в `/admin/oauth-pending`, проходит руками
- `/oauth/callback` (api) обменивает code на token

### Debug capture

При любой ошибке делаем screenshot + HTML dump в `/app/debug/<address>/
<timestamp>.{png,html,txt}`. Volume mounted `./debug:/app/debug`. Файлы в
git-ignore.

### Microsoft proof-confirmation challenge

При login с unfamiliar IP MS показывает экран "Verify your email" с
`data-testid="proof-confirmation-email-input"`. Worker умеет его
обнаружить и заполнить:

```python
proof_input = page.locator(
    '#proof-confirmation-email-input, '
    '[data-testid="proof-confirmation-email-input"] input'
)
if await proof_input.count() > 0:
    await proof_input.first.fill(attempt.address)
    await page.click('[data-testid="primaryButton"] button')
```

**НО**: MS ожидает **alternate (recovery) email**, не primary. Сейчас
worker заполняет primary → MS отвергает. Чтобы починить — нужно
`recovery_email` field на mailbox row + recursive flow (см. root CLAUDE.md
секция 10.4).

## OAUTH REFRESH

`OAuthRefresher` каждые 60 сек:
1. SELECT mailboxes WHERE status='oauth_active' AND access_token_expires_at
   < NOW() + 10min OR access_token IS NULL
2. Для каждого — POST на `/token` с `grant_type=refresh_token`
3. Сохранить новый access_token + ротированный refresh_token

Microsoft **ротирует** refresh_token каждый refresh — старый умирает.
Google ротирует **иногда**. Всегда сохраняем `refresh_token` если в
ответе есть, иначе keep existing.

При неудаче — записываем `oauth_last_error`, статус остаётся
`oauth_active` (мы не хотим автомата flip'ать в "broken"). Если ошибка
persistent — оператор увидит в admin UI на mailbox detail page.

## FORWARDING PROBE

Раз в 3 дня для каждого mailbox в `rely_on_groups`:
1. Достать SMTP-credentials группы (тот же ящик что слушаем IMAP'ом)
2. SMTP STARTTLS login → отправить test email на target hotmail
3. Ждать до 3 минут пока письмо появится в group inbox через forwarding
4. Записать результат `forwarding_probe_status='ok'|'failed'`

Использовали SMTP **самой группы** как from — иначе на DKIM/SPF
поломаются. Subject содержит unique token, **но** не используем его для
матчинга — просто смотрим есть ли новые письма для target за окно.

## FORWARDING SETUP

`forwarding_setup.py::configure_for_mailbox()` — **разовая** операция
(не loop). Triggered via:
- `POST /api/v1/mailboxes/<addr>/setup-forwarding` (admin-scope key)
- `POST /admin/mailboxes/<addr>/setup-forwarding` (admin UI button)

Запускает Playwright (patchright) → outlook.live.com login → navigate
to forwarding settings page → toggle ON → fill target → Save.

Best-effort. Если MS меняет селекторы / шлёт challenge — записывает
`forwarding_probe_error`. Юзер может настроить руками.

## NORMALIZER + EXTRACTOR + CODE_WRITER

Pipeline:

```
RFC822 bytes ──► normalizer.parse_rfc822()
                    │
                    ▼
NormalizedMessage(sender, recipient, subject, body_text, message_id, raw_to_header)
                    │
                    ▼
extractor.run_extraction(sender, subject, body, patterns=...)
                    │
                    ▼
ExtractionResult(code, platform, matched_pattern_id, candidates=[debug list])
                    │
                    ▼
code_writer.process_and_store()
    • resolve tenant_id (target → source fallback)
    • INSERT into codes ON CONFLICT (target_address, message_id) DO NOTHING
    • UPDATE mailboxes SET last_code_at
    • pg_notify('codes_tenant_<id>', code_id)
```

### Extractor pure-функция

`run_extraction()` не делает IO. Принимает list of pattern records
(dict-like), возвращает result. Tests'абельная, использовалась в
`/admin/extractors/playground`.

### Dedup ловушка

Уникальный индекс на `(target_address, message_id) WHERE message_id IS
NOT NULL`. Если письмо приходит **дважды** (например, и direct IMAP, и
через group inbox — что нормально для mode=both) — второй раз `ON
CONFLICT DO NOTHING`. Если у письма нет Message-ID (rare, но бывает) —
дедупа нет, может попасть несколько раз.

## ДОБАВЛЕНИЕ НОВОГО WORKER'А

1. Создай `workers/your_worker.py` с классом + `run()` + `stop()`
2. В `workers/main.py` добавь:
   ```python
   yw = YourWorker(pool)
   tasks.append(asyncio.create_task(yw.run(), name="your_worker"))
   ```
   + добавь `await yw.stop()` в shutdown chain
3. Если нужны новые DB-поля — migration в `db/migrations/`
4. Если worker меняет mailbox.status — обнови `_decide_status` в
   `codecatch/mailbox_service.py` + `imap_worker.reconcile_workers` query
   если worker должен после нового статуса подхватываться

## ТЕСТЫ

Сейчас только smoke-imports. Когда добавим — `extractor.run_extraction`
самая тестабельная (pure function), нормализатор тоже. IMAP/OAuth worker'ы
интеграционные, нужна mock IMAP-сервер или test-account.
