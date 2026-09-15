# Rabota.md HTTP (waf_http) — основной транспорт без Chromium, stealth-browser как fallback

Дизайн-документ. Дата разведки: 2026-09-03. Spike: 2026-09-12 (успешен).
Статус: spike пройден, архитектура пересмотрена в пользу primary waf_http; implementation spec уточнён 2026-09-12, реализация не начата.

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

Solver [Switch3301/Aws-Waf-Solver](https://github.com/Switch3301/Aws-Waf-Solver)
(~490 строк Python; pinned tree не содержит LICENSE, см. `waf/UPSTREAM.md`) доработан и прогнан против rabota.md. Результаты:

1. **3/3 последовательных solve**, каждый подтверждён GET: категория `it` → `200`,
   101 уникальный ID вакансии; detail → `200`, `.vacancy-content` + JSON-LD `JobPosting`.
2. **TLS-impersonation не нужна**: порт solver'а с `rnet` на чистый `httpx` минтит
   валидный токен (~0.6 с) — новых зависимостей в прод-образе не требуется
   (`httpx`, `cryptography`, `structlog` уже в pyproject; scrypt — stdlib).
3. **`pyscrypt` использовать нельзя**: 314 мс/итерацию (HashcashScrypt d=8 → 297 с).
   `hashlib.scrypt` (OpenSSL) — 0.42 мс/итерацию, бит-в-бит паритет проверен.
   PoW-бюджет 30 с перекрывает любую разумную difficulty.
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
        |  RabotaMdFetcher: get(url) / post_html_fragment(url)
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
| (httpx + cookie) | <----------------------------- |  get/publish/invalidate |
+------------------+   aws-waf-token (single-flight)+----------------------+
                                                             |
             +-------------------+  +-------------------+  +------------------+
             | PurePythonSolver  |  | StealthBrowser    |  | EnvTokenProvider |
             | (vendored, provenance tracked) |  | TokenMinter       |  | (аварийный, ops) |
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
- request-level browser fallback разрешён только для повторного
  `202 + x-amzn-waf-action: challenge` после успешного token refresh и для подтверждённой
  поломки AJAX POST-контракта (`403` при уже каноническом наборе заголовков);
- `403` не ретраится вслепую теми же заголовками: сначала проверяется, что запрос действительно
  использовал канонический AJAX header set; если нет — это bug/config error, если да — допускается
  один request-level browser fallback;
- `429` **не является сигналом browser fallback**: соблюдается `Retry-After`/backoff, чтобы
  браузер не превращался в способ обхода server-side rate limit;
- на fallback-сигнале тот же запрос повторяется через `StealthPlaywrightBrowser`; ответ
  возвращается адаптеру как обычно — parsing общий, checkpoint и итератор не затронуты;
- если browser fallback успешно прошёл WAF и получил новый `aws-waf-token`, токен
  обязательно публикуется обратно в `WafTokenProvider`/Redis и синхронизируется с cookie
  jar `WafHttpClient`; следующий запрос снова идёт через HTTP, а не закрепляет scan на браузере;
- факт переключения пишется в метрику `rabota_md_transport_fallback_total` и
  structured log (без токена); за scan допускается ограниченное число переключений
  (предлагается 20), дальше — `RabotaMdDegradedError` наружу (реальная деградация);
- если `playwright` extra недоступен (`BrowserFallbackUnavailable`), fallback-ветка
  пропускается, и primary-ошибка пробрасывается как сейчас.


#### WAF error taxonomy и state machine

Ошибки WAF разделяются явно; общий `WafSolverError` не используется как универсальный
сигнал «попробовать браузер», чтобы CAPTCHA/block случайно не попали в fallback-ветку:

```text
WafChallengeRequired
    -> invalidate/refresh token -> HTTP retry once

WafUnsupportedChallenge / WafScriptVersionUnknown / WafPowTimeout
    -> pure-Python solver unavailable
    -> StealthBrowserTokenMinter разрешён

WafCaptchaRequired
    -> FAIL CLOSED + alert
    -> browser fallback/minter запрещён

WafBlocked
    -> FAIL CLOSED + alert
    -> browser fallback/minter запрещён

WafRateLimited
    -> Retry-After/backoff
    -> browser fallback запрещён

WafPostContractError
    -> один request-level browser fallback, только если canonical AJAX headers уже были применены
```

Две роли Chromium не должны запускаться подряд без необходимости. Нормальная цепочка:

```text
HTTP request
    -> 202 challenge
    -> WafTokenProvider.refresh()
        -> PurePythonSolver
        -> unsupported script/challenge -> StealthBrowserTokenMinter
    -> publish token -> sync HTTP cookie jar
    -> retry original HTTP request once
    -> если request всё ещё не обслуживается и ошибка допускает request fallback
       -> FallbackFetcher делает этот один запрос через browser
       -> если browser получил свежий token, publish/sync его
    -> следующий запрос снова начинается с HTTP
```

`StealthBrowserTokenMinter` и request-level browser fallback — разные операции. Успешный
browser mint не является основанием сразу повторять тот же запрос браузером: сначала
обязателен HTTP retry с новым токеном.

### Transport contract и WafHttpClient

Текущий общий `HttpFetcher` формально содержит только `get()`, тогда как Rabota.md уже
использует `post_html_fragment()` через `getattr()`. Перед реализацией вводится отдельный
`RabotaMdFetcher` protocol с `get()`, `post_html_fragment()` и `aclose()`. Общий
`HttpFetcher` не расширяется AJAX-спецификой Rabota.md.

`WafHttpClient` реализует `RabotaMdFetcher` поверх безопасных примитивов `SecureHttpClient`:

- один persistent cookie jar с `aws-waf-token`; токен подставляется только в запросы к
  `*.rabota.md`;
- `get(url)` — обычный GET; ответ `202` + `x-amzn-waf-action: challenge` (детект по
  статусу и заголовку, не по телу — см. spike п. 7) трактуется как протухший токен:
  invalidate → single-flight refresh → один retry. Повторный challenge после refresh —
  сигнал `FallbackFetcher`, затем `RabotaMdDegradedError`;
- `post_html_fragment(url)` — POST с фиксированным браузерным набором заголовков из
  п. 6 разведки (Referer вычисляется из URL страницы 1 категории); контракт ответа
  (`success=true`, строковый `data.content`) совпадает с браузерной реализацией, поэтому
  parsing адаптера не меняется;
- `SecureHttpClient` получает внутренний bounded request primitive для строго разрешённых
  `GET`/`POST`, чтобы не дублировать SSRF, redirect и response-size guards в WAF-клиенте;
  наружу generic arbitrary-method API не экспортируется;
- rate limit, allowlist доменов, SSRF-валидация URL и redirect-проверки — те же, что у
  `SecureHttpClient` (50 rpm, интервал ≥ 1.2 с, только `rabota.md`/`www.rabota.md`);
- текущий `SecureHttpClient` при DNS/IP pinning намеренно отключает keep-alive, поэтому
  ожидаемый выигрыш `waf_http` основан прежде всего на отсутствии Chromium/DOM/JS, а не
  на обещаниях connection pooling;
- `token.awswaf.com`/`captcha.awswaf.com` в allowlist нужны только solver'у
  (см. ниже), не crawl-трафику.

#### Инцидент 2026-09-15: привязка токена к User-Agent

AWS WAF включил на rabota.md проверку соответствия User-Agent запроса тому UA, с
которым был выминчен `aws-waf-token` (rollout: первый сбой в 01:00 UTC, стабильно с
08:00 UTC). Симптом: `GET` страницы 1 категории проходит, а paginated `GET`/`POST`
получают голый `403` от ELB без заголовка `x-amzn-waf-action`. Контрольный
эксперимент: один и тот же токен — `200` с UA минта, `403` с любым другим UA,
снова `200` при возврате исходного UA.

Выводы, зафиксированные в реализации:

- **один UA для минта и всех crawl-запросов**: `effective_waf_user_agent()` в
  `transport.py` — browser-shaped UA используется как есть, идентифицирующий
  `job-agent/...` дописывается суффиксом к browser-базе (полностью синтетический
  UA отклоняется WAF на paginated путях даже с привязанным токеном — проверено
  live); solver, `SecureHttpClient` и canary probe используют только эту строку;
- **голый 403 без action-заголовка = token rejection**, а не hard block: тот же
  путь invalidate → refresh → один retry; повторный 403 после refresh —
  `WafChallengeRequired` (fallback-allowed). Явный `x-amzn-waf-action: block`
  остаётся fail-closed;
- **републикация браузерного токена в provider — только при совпадении UA**
  браузера и HTTP-транспорта: браузерный токен привязан к UA Chromium, с
  crawl-UA он гарантированно получит 403;
- токены из `EnvTokenBackend` обязаны быть выминчены с тем же effective UA.

### WafTokenProvider

Протокол минимум `async get_token() -> str`, `async refresh_token() -> str`,
`async publish_token(token, expires)` и `async invalidate()`. Хранилище — Redis
(`redis.asyncio`, уже используется в проекте): ключ `crawler:rabota_md:waf_token`, lock-key
`crawler:rabota_md:waf_token:refresh_lock`. TTL вычисляется консервативно как
`min(cookie_expiry - safety_margin, configured_max_ttl)`; cookie expiry не считается
гарантией реального AWS immunity window.

Single-flight refresh имеет фиксированную семантику:

1. прочитать token; если он годен — вернуть его без lock;
2. если token отсутствует/invalid — попытаться взять Redis lock с обязательным TTL;
3. **после получения lock перечитать token**: другой worker мог уже завершить refresh;
4. только если token всё ещё отсутствует/invalid — выполнить mint и `publish_token`;
5. при crash lock сам истекает по TTL; вечный lock запрещён.

Токен — чувствительное значение: не логируется, не попадает в audit/observability и не
коммитится.

Backends (в порядке приоритета):

1. **`PurePythonSolver`** — основной, результаты spike'а. Upstream для воспроизводимости
   жёстко pin'ится на `Switch3301/Aws-Waf-Solver@fed489c54fe2eb10a6dfac5b4d4c5dfcb06b8808`
   (provenance в `waf/UPSTREAM.md`; license review обязателен до PROD/distribution); не брать свежий `main/master` во время реализации. Вендорятся в
   `app/crawlers/adapters/rabota_md/waf/`: `solver.py`, `crypto.py`, `signal.py`,
   `metrics.py`, `webgl.json`. Важно: подтверждённый plain-`httpx` spike был отдельным
   портом `/tmp/waf-solvers/test_httpx_port.py`, который переиспользует helper'ы upstream,
   но **не** является неизменённым upstream `solve()`. Реализация должна перенести именно
   проверенную транспортную схему spike'а, а не копировать свежий upstream вслепую.
   Обязательные правки при вендоринге (проверены spike'ом):
   - `rnet` → `httpx` (impersonation не нужна);
   - `pyscrypt` → `hashlib.scrypt` (750× быстрее, паритет подтверждён);
   - бюджет времени на PoW (30 с) — превышение = `WafPowTimeout`; browser token minter
     разрешён, request-level fallback решается отдельно по state machine выше;
   - `x-amzn-waf-action: captcha` → `WafCaptchaRequired`, `block` → `WafBlocked`: оба
     случая fail-closed, без browser fallback/minter; неизвестная версия challenge/script
     → `WafUnsupportedChallenge`/`WafScriptVersionUnknown` и может использовать только
     разрешённый browser token minter;
   - сетевые вызовы solver'а (challenge page, `challenge.js`, token endpoint) проходят
     тот же rate limiter; allowlist solver'а = `*.rabota.md` + `*.token.awswaf.com`.
2. **`StealthBrowserTokenMinter`** — fallback backend. Поднимает существующий
   `StealthPlaywrightBrowser` на одну навигацию `/ru/vacancies`, ждёт разрешения
   challenge (механика уже реализована в `StealthPlaywrightBrowser.get`), забирает cookie
   `aws-waf-token` из контекста (нужен небольшой метод чтения cookies в `browser.py`) и
   закрывает браузер. Успешно полученный токен публикуется в общий provider, чтобы
   последующий crawl немедленно вернулся на HTTP. Запускается по сигналу invalidate,
   если solver недоступен.
3. **`EnvTokenProvider`** — аварийный ручной канал: токен из переменной окружения/
   secret-файла, выпущенный оператором из обычного браузера. Позволяет пережить поломку
   обоих backend'ов без деплоя.

### ScriptWatchdog и canary

Live E2E показал, что AWS генерирует разные байты `challenge.js` между совместимыми
solve, поэтому exact SHA нельзя использовать как version allowlist. Защита строится иначе:

- ежедневный live canary выполняет настоящий solve вне scan; успех пишет в Redis
  `solver_canary_ok` с TTL 30 часов;
- marker содержит versioned protocol fingerprint. После изменения protocol assumptions
  старый marker больше не разрешает pure solver;
- SHA текущего `challenge.js` сохраняется только в логах/canary payload для диагностики и
  никогда не является gate;
- без свежего canary marker pure solver fail-closed и токен минтит
  `StealthBrowserTokenMinter`; `captcha`/`block` по-прежнему не допускают browser bypass;
- сам solver остаётся структурным guard: разрешённые URL, response taxonomy, challenge type,
  inputs/verify/token contract проверяются и при несовместимости завершаются fail-closed.

### Изменения в адаптере и конфиге

`RabotaMdAdapter` уже изолирует parsing от транспорта, но перед реализацией его
transport type уточняется до `RabotaMdFetcher`. Parsing/entrypoints/checkpoint semantics
не меняются; меняется выбор транспорта по конфигу (`RabotaMdConfig`):

```yaml
source:
  id: rabota_md
  adapter: rabota_md
  transport: waf_http                   # waf_http | stealth_browser
  fallback_transport: stealth_browser   # stealth_browser | none
  # ... остальные ключи без изменений
```

- `transport: waf_http` (целевой дефолт) — `FallbackFetcher(WafHttpClient, fallback)`;
- `fallback_transport: stealth_browser` — in-scan fallback + аварийный минтер;
  `none` — строгий режим без браузера (когда playwright extra удалён из образа);
- `transport: stealth_browser` — текущее поведение, без изменений (откат конфигом);
- устаревший флаг `use_stealth_browser` учитывается **только если `transport` отсутствует**:
  `true` → `stealth_browser`, `false` → `waf_http` без fallback;
- если одновременно заданы новый `transport` и legacy `use_stealth_browser` и они
  противоречат друг другу, конфигурация fail-fast отклоняется вместо тихого выбора.

### Границы доверия и безопасность

- Те же fail-closed правила: повторный challenge после refresh/fallback, `403` после
  проверки POST-контракта и `5xx` становятся degraded/temporary ошибками, никогда
  «пустой выдачей»; `429` отдельно соблюдает `Retry-After`/backoff и **не** включает
  browser fallback; массовые переходы статусов при деградации отключены.
- Токен не расширяет права: он лишь воспроизводит сессию обычного посетителя публичных
  страниц. Allowlist публичных путей (`_allowed_public_path`) остаётся без изменений,
  внутренние endpoints (`/ajax/`, `/cabinet/`, …) по-прежнему запрещены, кроме уже
  существующего строго ограниченного same-site pagination POST-контракта адаптера.
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
- Привязка токена к User-Agent подтверждена инцидентом 2026-09-15 (см. одноимённый
  раздел); возможная ужесточающая привязка к IP/поведению отслеживается метрикой
  «доля 202/403 по транспорту» — обязательный сигнал регресса.
- POST-контракт пагинации (заголовки, форма JSON) — внутренний контракт сайта; его смена
  ломает оба транспорта одинаково, детектится существующими parse-error метриками.
- **Browser fallback — только GET-пути** (исследование 2026-09-15 вечером): AWS WAF
  требует CAPTCHA на paginated путях (`/vacancies/category/*/N`, GET и POST одинаково)
  для любых Chromium-сессий с нашего IP. Исчерпывающе проверено и отвергнуто:
  in-page `fetch` с каноническими заголовками, байт-в-байт родной `jQuery.ajax` сайта,
  инъекция solver-токена в браузерный cookie-jar, browser-токен в httpx,
  `--disable-http2` (h1 против h2), headed Chromium под Xvfb, playwright_stealth
  (webdriver=false, WebGL spoofed). Рабочая матрица:
  solver-токен + plain httpx = 200; любая комбинация с Chromium (минт или транспорт)
  = 405 + `x-amzn-waf-action: captcha`. Вывод: paginated путь браузера не поднимается
  никакими браузерными твиками — это репутационное правило WAF, а не детект headless.
  Реальные аварийные опции для пагинации: `EnvTokenBackend` с токеном из настоящего
  residential Chrome (оператор) или residential proxy для fallback-браузера
  (крюк `JOBHUNTER_BROWSER_PROXY` уже есть) — обе решения оператора, не кода.
  GET-пути (страница 1 категорий, детальные страницы) браузерный fallback
  обслуживает штатно — проверено live.
- Fingerprint solver'а — один статический профиль (`signal.py`), solve быстрее реального
  браузера (~0.2 с). Поведенческие эвристики сервера отслеживаются метрикой
  `waf_solver_solve_duration_seconds` и долей fallback-переключений.

## План миграции (пересмотрен после spike: primary-first)

1. **Фаза 1 — transport foundation:** сначала `RabotaMdFetcher` protocol и bounded
   `GET`/`POST` primitive в `SecureHttpClient`, затем `WafHttpClient` с fake token provider
   и тестами `202/403/429/AJAX`. После стабилизации интерфейса — `WafTokenProvider`
   (Redis store, refresh/publish/invalidate, single-flight с double-read после lock), затем
   vendored solver строго от pinned commit `fed489c54fe2...` и проверенного `httpx` spike-port,
   `StealthBrowserTokenMinter`, `FallbackFetcher`, ScriptWatchdog + canary-задача и ключи
   `transport`/`fallback_transport` в конфиге. Unit-тесты на существующих fixtures через
   injected fetcher; live smoke ограниченного числа вакансий без откликов.
2. **Фаза 2 — включение primary:** `transport: waf_http` + `fallback_transport:
   stealth_browser` на проде после стандартного deployment gate. Браузер остаётся в
   образе и страхует каждый scan. Наблюдение ≥ 2 недель: **≥99% crawl-request'ов
   обслуживаются без Chromium**, fallback остаётся редким, нет необъяснимого 202/403
   шторма; success rate solver'а отслеживается как вторичная метрика вместе с частотой
   token refresh, потому что при многосуточном токене solver в норме вызывается редко.
3. **Фаза 3 — решение об удалении браузера:** вынос минтинга из приложения
   (`EnvTokenProvider` + ops-процедура) либо сохранение минимального browser-extra как
   fallback. Только после этого `playwright`/`playwright-stealth` исключаются из
   production-образа, `fallback_transport: none` становится дефолтом.

## Проверки

- unit: parity parsing одних и тех же fixture HTML обоими транспортами; обновление токена
  по 202 (детект по статусу+заголовку); `403` на POST с неканоническими headers считается
  bug/config error без бессмысленного retry, а `403` при канонических headers допускает один
  request-level browser fallback; `429` соблюдает backoff и не вызывает browser fallback;
  single-flight refresh перечитывает token после lock; browser fallback после
  успешного WAF-прохождения публикует токен обратно в Redis/HTTP cookie jar; переключение
  `FallbackFetcher` на браузер при повторном challenge; missing/expired compatibility canary
  блокирует pure-Python solver; `captcha`/`block` не могут попасть ни в browser
  minter, ни в request-level fallback; `StealthBrowserTokenMinter` после успеха приводит к
  HTTP retry до любого request-level fallback; конфликт нового transport-конфига с
  legacy-флагом fail-fast; отсутствие токена в логах; scrypt через
  `hashlib.scrypt`.
- integration: live smoke `waf_http` — listing, 2+ страницы AJAX-пагинации, detail,
  recheck; сверка `content_hash` с browser-транспортом на пересекающихся ID
  (спайк-сценарии уже отработаны в `/tmp/waf-solvers`, переносятся в `tests/realcall`).
- canary: ежедневный solve вне scan с алертом при failure; failed/expired compatibility canary блокирует solver в scan;
  динамический script SHA остаётся observability-only.
- observability: метрики `rabota_md_waf_token_refresh_total`,
  `rabota_md_waf_challenge_total`, `rabota_md_transport_fallback_total`,
  `waf_solver_attempts_total`, `waf_solver_success_total`,
  `waf_solver_solve_duration_seconds`, `waf_solver_compatibility`,
  `rabota_md_http_without_browser_ratio`, частота token refresh и доля degraded по
  транспортам.
- implementation scope: изменения выполняются **только в DEV checkout** `/home/andrei/JobHunter`.
  Во время реализации нельзя редактировать `/srv/jobhunter-prod`, pull/build/restart/migrate/deploy
  PROD или менять production secrets. PROD mutation требует отдельной явной авторизации оператора
  после завершения всех gate-проверок.
- обязательные project checks перед handoff: `ruff check .`, `ruff format --check .`,
  `mypy app fixture_site`, `pytest`. Обычный `pytest` не должен включать live crawling.
- live Rabota smoke остаётся строго opt-in через существующий gate
  `ENABLE_LIVE_RABOTA_SMOKE_TEST=true`; live-тест не включается автоматически и не заменяет
  non-production validation.
- browser/cookie/WAF integration-sensitive path перед PROD должен пройти end-to-end в DEV/non-PROD
  **3 раза подряд** с чистым browser context согласно `AGENTS.md`.
- после тестов исполнитель должен остановиться, перечислить изменённые файлы, результаты проверок,
  rollout/rollback plan и ждать отдельной авторизации на PROD; никакого самовольного deploy.
