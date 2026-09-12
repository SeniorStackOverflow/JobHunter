# Rabota.md HTTP (waf_http) — основной транспорт без Chromium, stealth-browser как fallback

Дизайн-документ. Дата разведки: 2026-09-03. Spike: 2026-09-12 (успешен).
Статус: spike пройден, архитектура пересмотрена в пользу primary waf_http, реализация не начата.

## Цель

Убрать тяжёлый stealth-Chromium (`playwright` + `playwright-stealth`) из горячего пути
crawling Rabota.md. Транспорт `waf_http` работает на plain HTTP (`httpx`) и является
**основным**. Stealth-Chromium остаётся в двух вспомогательных ролях:

1. **In-scan fallback**: при деградации `waf_http` (повторный challenge после refresh,
   таймаут PoW, незнакомый challenge) тот же запрос прозрачно переключается на браузер —
   переключение невидимо для pipeline и не ставит источник на паузу;
2. **Аварийный минтер токена**: fallback-backend `WafTokenProvider`, если pure-Python
   solver сломан ротацией `challenge.js`.

Источник один (`rabota_md`), оба транспорта живут внутри него. Отдельный shadow-источник
не используется: семантика «fallback в том же scan» достижима только внутри адаптера,
а двойной crawl удвоил бы нагрузку на сайт.

## Spike 2026-09-12: что проверено live

Solver [Switch3301/Aws-Waf-Solver](https://github.com/Switch3301/Aws-Waf-Solver) (MIT,
~490 строк Python) доработан и прогнан против rabota.md. Результаты:

1. **3/3 последовательных solve**, каждый подтверждён GET: категория `it` → `200`,
   101 уникальный ID вакансии; detail → `200`, `.vacancy-content` + JSON-LD `JobPosting`.
2. **TLS-impersonation не нужна**: порт solver'а с `rnet` на чистый `httpx` минтит
   валидный токен (~0.6 с) — новых зависимостей в прод-образе не требуется
   (`httpx`, `cryptography`, `structlog` уже в pyproject; scrypt — stdlib).
3. **`pyscrypt` использовать нельзя**: 314 мс/итерацию (HashcashScrypt d=8 → 297 с).
   `hashlib.scrypt` (OpenSSL) — 0.42 мс/итерацию, бит-в-бит паритет проверен.
   PoW-bюджет 30 с перекрывает любую разумную difficulty.
4. **Переносимость токена подтверждена повторно**: токен принимается plain `httpx`
   с UA `job-agent/0.1`; 3 конкурентных solve дают 3 валидных токена.
5. **AJAX-пагинация POST** работает с токеном и браузерным набором заголовков:
   `200`, `{"success": true, "data": {"content": ...}}`, 95 ID на странице 2.
6. **Сайт стабильно выдаёт NetworkBandwidth difficulty=1** (~0.2 с на solve).
   Типы SHA256/HashcashScrypt в коде solver'а проверены offline на корректность nonce.
7. **Тело 202-страницы зависит от `Accept`**: с голым UA `job-agent/0.1` тело пустое;
   `gokuProps` и URL `challenge.js` приходят только с браузерным `Accept: text/html,...`.
   Детект протухшего токена — **только по статусу 202 + заголовку
   `x-amzn-waf-action: challenge`**, никогда по телу.
8. **`/sitemap.xml` отдаёт 404** даже с валидным токеном — в entrypoints его нет
   (п. 1 разведки 2026-09-03 устарел в этой части).
9. Снапшот для ScriptWatchdog: `challenge.js` = 691 453 B,
   sha256 `b000d2af5018f44f...`, URL
   `https://1d77315990cc.7438b4fd.eu-central-1.token.awswaf.com/.../challenge.js`.

## Результаты разведки live-сайта (2026-09-03)

Все проверки выполнялись read-only, без откликов на вакансии, с идентифицирующим
User-Agent `job-agent/0.1` либо браузерным UA. Выводы:

1. **Весь сайт за AWS WAF challenge.** Любой GET (`/`, `/ru/vacancies`, `robots.txt`,
   даже `api.rabota.md`) без валидной cookie возвращает `HTTP 202` + заголовок
   `x-amzn-waf-action: challenge` и challenge-страницу с `window.gokuProps`.
2. **TLS-фингерпринт не решает.** `curl_cffi` с impersonation получает тот же 202.
   Гейт — только cookie. (Подтверждено spike'ом и в обратную сторону: для минтинга
   impersonation тоже не нужна.)
3. **Токен переносим между клиентами** (см. spike п. 4). Привязки к TLS/UA нет;
   привязка к IP не наблюдалась, но считается риском под наблюдением.
4. **TTL токена ≈ 4 суток.** Cookie `aws-waf-token` (domain `.www.rabota.md`, `Secure`,
   `SameSite=Lax`). Реальный immunity window сайт может уменьшить в любой момент.
5. **Все три нагрузки работают по plain HTTP с токеном:** listing, POST пагинации,
   detail (`.vacancy-content`, `h1.vacancy-title`, JSON-LD, `mailto:`/`tel:`).
6. **POST требует браузерного набора заголовков** (`X-Requested-With`, `Origin`,
   `Referer`, `Accept`, `Sec-Fetch-*`), без них — `403 Forbidden` (не WAF).
7. **Невалидный токен предсказуем:** `202` + challenge — надёжный сигнал refresh.
8. **Headless Chrome без stealth не проходит** (WAF детектирует автоматизацию),
   поэтому минтер использует существующий `StealthPlaywrightBrowser`.

## Архитектура

```text
RabotaMdAdapter (без изменений: entrypoints, пагинация, нормализация, статусы)
        |
        |  get(url) / post_html_fragment(url)   — seam уже существует
        v
+---------------------------+     деградация primary      +---------------------------+
|      FallbackFetcher      | --------------------------> | StealthPlaywrightBrowser  |
|  (невидим для pipeline,   |  тот же запрос, тот же scan | (in-scan fallback +       |
|   checkpoint не трогается)| <-------------------------- |  аварийный минтер токена) |
+-------------+-------------+   HTML браузера проходит    +---------------------------+
              |                 тот же parsing
              v
+------------------+      202 challenge / 403       +----------------------+
|  WafHttpClient   | -----------------------------> |   WafTokenProvider   |
| (httpx + cookie) | <----------------------------- |  get / invalidate    |
+------------------+   aws-waf-token (single-flight)+----------------------+
                                                             |
             +-------------------+  +-------------------+  +------------------+
             | PurePythonSolver  |  | StealthBrowser    |  | EnvTokenProvider |
             | (vendored, MIT)  |  | TokenMinter       |  | (аварийный, ops) |
             +-------------------+  +-------------------+  +------------------+
```

### FallbackFetcher — ключевое отличие от первой версии дизайна

Первая версия документа предполагала `auto` = browser primary, `waf_http` резерв.
Spike инвертировал решение: **primary = `waf_http`, fallback = stealth-browser**.

Почему fallback внутри адаптера, а не снаружи: любое исключение из итератора адаптера
в scan pipeline (`app/crawlers/pipeline.py`) переводит run в PARTIAL, источник в
`DEGRADED` + `automatic_actions_paused` и создаёт high alert. Если бы переключение
транспорта происходило снаружи, каждая деградация `waf_http` ставила бы источник на
паузу до того, как браузер успел бы помочь. `FallbackFetcher` перехватывает деградацию
**до** исключения:

- `get(url)` / `post_html_fragment(url)` сначала идут через `WafHttpClient`;
- сигналы переключения: повторный 202 challenge после refresh, `403` на POST после
  retry, таймаут PoW-бюджета, `WafSolverError` (незнакомый challenge/captcha-страница);
- на сигнале тот же запрос повторяется через `StealthPlaywrightBrowser`; ответ
  возвращается адаптеру как обычно — parsing общий, checkpoint и итератор не затронуты;
- факт переключения пишется в метрику `rabota_md_transport_fallback_total` и
  structured log (без токена); за scan допускается ограниченное число переключений
  (предлагается 20), дальше — `RabotaMdDegradedError` наружу (реальная деградация);
- если `playwright` extra недоступен (`BrowserFallbackUnavailable`), fallback-ветка
  пропускается, и primary-ошибка пробрасывается как сейчас.

### WafHttpClient

Реализация существующего seam `HttpFetcher` поверх `SecureHttpClient`:

- один persistent cookie jar с `aws-waf-token`; токен подставляется только в запросы к
  `*.rabota.md`;
- `get(url)` — обычный GET; ответ `202` + `x-amzn-waf-action: challenge` (детект по
  статусу и заголовку, не по телу — см. spike п. 7) трактуется как протухший токен:
  invalidate → single-flight refresh → один retry. Повторный challenge после refresh —
  сигнал `FallbackFetcher`, затем `RabotaMdDegradedError`;
- `post_html_fragment(url)` — POST с фиксированным браузерным набором заголовков из
  п. 6 разведки (Referer вычисляется из URL страницы 1 категории); контракт ответа
  (`success=true`, строковый `data.content`) совпадает с браузерной реализацией, поэтому
  код адаптера не меняется;
- rate limit, allowlist доменов, SSRF-валидация URL и redirect-проверки — те же, что у
  `SecureHttpClient` (50 rpm, интервал ≥ 1.2 с, только `rabota.md`/`www.rabota.md`);
- `token.awswaf.com`/`captcha.awswaf.com` в allowlist нужны только solver'у
  (см. ниже), не crawl-трафику.

### WafTokenProvider

Протокол `async get_token() -> str` / `async invalidate()`. Хранилище — Redis
(`redis.asyncio`, уже используется в проекте): ключ `crawler:rabota_md:waf_token` с TTL
из `expires` cookie минус защитный запас (12 часов). Обновление — single-flight через
Redis lock, чтобы параллельные ветки scan не минтили токен одновременно. Токен —
чувствительное значение: не логируется, не попадает в audit/observability, не коммитится.

Backends (в порядке приоритета):

1. **`PurePythonSolver`** — основной, результаты spike'а. Вендорится из
   [Aws-Waf-Solver](https://github.com/Switch3301/Aws-Waf-Solver) (MIT, с атрибуцией) в
   `app/crawlers/adapters/rabota_md/waf/`: `solver.py`, `crypto.py`, `signal.py`,
   `metrics.py`, `webgl.json`. Обязательные правки при вендоринге (проверены spike'ом):
   - `rnet` → `httpx` (impersonation не нужна);
   - `pyscrypt` → `hashlib.scrypt` (750× быстрее, паритет подтверждён);
   - бюджет времени на PoW (30 с) — превышение = `WafSolverError` → fallback-цепочка;
   - `x-amzn-waf-action: captcha`/`block`/незнакомый action — немедленный стоп,
     никаких попыток решать (граница проекта);
   - сетевые вызовы solver'а (challenge page, `challenge.js`, token endpoint) проходят
     тот же rate limiter; allowlist solver'а = `*.rabota.md` + `*.token.awswaf.com`.
2. **`StealthBrowserTokenMinter`** — fallback backend. Поднимает существующий
   `StealthPlaywrightBrowser` на одну навигацию `/ru/vacancies`, ждёт разрешения
   challenge (механика уже реализована в `StealthPlaywrightBrowser.get`), забирает cookie
   `aws-waf-token` из контекста (нужен небольшой метод чтения cookies в `browser.py`) и
   закрывает браузер. Запускается по сигналу invalidate, если solver недоступен.
3. **`EnvTokenProvider`** — аварийный ручной канал: токен из переменной окружения/
   secret-файла, выпущенный оператором из обычного браузера. Позволяет пережить поломку
   обоих backend'ов без деплоя.

### ScriptWatchdog и canary

Pure-Python solver реимплементирует протокол конкретной версии `challenge.js` — ротация
скрипта AWS ломает его мгновенно и потенциально молча. Смягчение:

- `sha256(challenge.js)` хранится в Redis (стартовый пин — см. spike п. 9);
- новая версия скрипта не идёт в scan, пока не пройден canary-прогон (одиночный solve
  вне расписания, задача 1 раз/день) и не выставлен сигнал `solver_canary_ok`;
- при failed canary solver исключается из цепочки, токен минтит браузер, оператору —
  алерт: ротация скрипта превращается в алерт, а не в молча сломанный источник.

### Изменения в адаптере и конфиге

`RabotaMdAdapter` уже принимает `HttpFetcher` и содержит весь parsing — его код не
меняется. Меняется только выбор транспорта по конфигу (`RabotaMdConfig`):

```yaml
source:
  id: rabota_md
  adapter: rabota_md
  transport: waf_http               # waf_http | stealth_browser
  fallback_transport: stealth_browser   # stealth_browser | none
  # ... остальные ключи без изменений
```

- `transport: waf_http` (целевой дефолт) — `FallbackFetcher(WafHttpClient, fallback)`;
- `fallback_transport: stealth_browser` — in-scan fallback + аварийный минтер;
  `none` — строгий режим без браузера (когда playwright extra удалён из образа);
- `transport: stealth_browser` — текущее поведение, без изменений (откат конфигом);
- устаревший флаг `use_stealth_browser` маппится: `true` → `stealth_browser`,
  `false` → `waf_http` без fallback; сохраняется для обратной совместимости конфигов.

### Границы доверия и безопасность

- Те же fail-closed правила: 403/429/5xx и challenge после retry и fallback —
  degraded/temporary ошибки, никогда «пустая выдача»; массовые переходы статусов при
  деградации отключены.
- Токен не расширяет права: он лишь воспроизводит сессию обычного посетителя публичных
  страниц. Allowlist публичных путей (`_allowed_public_path`) остаётся без изменений,
  внутренние endpoints (`/ajax/`, `/cabinet/`, …) по-прежнему запрещены.
- Политика доступа не меняется: `policy_review_acknowledged` + `policy_review_reference`
  обязательны, живой трафик — умеренной интенсивности с теми же лимитами.
- HTML по-прежнему недоверенный ввод; parsing на selectolax и все нормализационные
  правила (контакты только из vacancy-owned блоков и т.д.) общие для обоих транспортов.
- Solver решает **только** `x-amzn-waf-action: challenge`; `captcha`/`block` — стоп и
  алерт, попытки решать CAPTCHA запрещены политикой проекта.

### Отличия от browser-транспорта, которые нужно держать под наблюдением

- Browser-транспорт отдаёт rendered DOM, HTTP — серверный HTML. Spike подтвердил:
  карточки листинга, `.vacancy-content`, JSON-LD и контакты рендерятся на сервере.
  Unit fixtures общие; live smoke сверяет оба транспорта на одних и тех же вакансиях
  (уже прогнан в spike — 101 ID на категории, detail с JSON-LD).
- Возможная скрытая привязка токена к IP/фингерпринту не наблюдалась, но сайт может её
  включить: метрика «доля 202/403 по транспорту» — обязательный сигнал регресса.
- POST-контракт пагинации (заголовки, форма JSON) — внутренний контракт сайта; его смена
  ломает оба транспорта одинаково, детектится существующими parse-error метриками.
- Fingerprint solver'а — один статический профиль (`signal.py`), solve быстрее реального
  браузера (~0.2 с). Поведенческие эвристики сервера отслеживаются метрикой
  `waf_solver_solve_duration_seconds` и долей fallback-переключений.

## План миграции (пересмотрен после spike: primary-first)

1. **Фаза 1 — реализация:** вендор solver'а в `app/crawlers/adapters/rabota_md/waf/`
   (правки выше), `WafTokenProvider` (Redis store, single-flight, цепочка backends),
   `WafHttpClient`, `FallbackFetcher`, ScriptWatchdog + canary-задача, ключи `transport`/
   `fallback_transport` в конфиге. Unit-тесты на существующих fixtures через injected
   fetcher; live smoke ограниченного числа вакансий без откликов.
2. **Фаза 2 — включение primary:** `transport: waf_http` + `fallback_transport:
   stealth_browser` на проде после стандартного deployment gate. Браузер остаётся в
   образе и страхует каждый scan. Наблюдение ≥ 2 недель: success rate solver'а ≥ 99%,
   доля fallback-переключений → 0, ни одного необъяснимого 202/403 шторма.
3. **Фаза 3 — решение об удалении браузера:** вынос минтинга из приложения
   (`EnvTokenProvider` + ops-процедура) либо сохранение минимального browser-extra как
   fallback. Только после этого `playwright`/`playwright-stealth` исключаются из
   production-образа, `fallback_transport: none` становится дефолтом.

## Проверки

- unit: parity parsing одних и тех же fixture HTML обоими транспортами; обновление токена
  по 202 (детект по статусу+заголовку); 403 на POST до retry-политики; single-flight
  refresh; переключение FallbackFetcher на браузер при повторном challenge; отказ при
  `captcha`-action; отсутствие токена в логах; scrypt через `hashlib.scrypt`.
- integration: live smoke `waf_http` — listing, 2+ страницы AJAX-пагинации, detail,
  recheck; сверка `content_hash` с browser-транспортом на пересекающихся ID
  (спайк-сценарии уже отработаны в `/tmp/waf-solvers`, переносятся в `tests/realcall`).
- canary: ежедневный solve вне scan с алертом при failure; смена content-hash скрипта без
  успешного canary блокирует solver в scan.
- observability: метрики `rabota_md_waf_token_refresh_total`,
  `rabota_md_waf_challenge_total`, `rabota_md_transport_fallback_total`,
  `waf_solver_attempts_total`, `waf_solver_success_total`,
  `waf_solver_solve_duration_seconds`, `waf_solver_script_version`,
  доля degraded по транспортам.
- деплой: только через стандартный production gate AGENTS.md (локальная валидация,
  e2e, явная авторизация оператора).
