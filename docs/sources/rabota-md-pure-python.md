# Rabota.md pure-Python AWS WAF solver — дизайн без браузера вообще

Дизайн-документ. Дата разведки: 2026-09-03/04. Статус: предложение, требует spike перед
принятием. Дополняет [rabota-md-http.md](rabota-md-http.md): тот документ убирает Chromium
из горячего пути crawl, этот — убирает браузер полностью, включая минтинг `aws-waf-token`.

## Постановка

Единственная причина, по которой в проекте вообще нужен браузер для Rabota.md, —
получение cookie `aws-waf-token`: AWS WAF отвечает `202` + challenge-страницей на любой
запрос без неё (см. разведку в [rabota-md-http.md](rabota-md-http.md)). Дальше весь crawl
уже работает на plain HTTP. Задача: решать WAF challenge на чистом Python, без Chromium,
Playwright и playwright-stealth.

## Факты разведки о challenge

- Challenge-страница содержит `window.gokuProps = { key, iv, context }` — зашифрованные
  входные параметры — и подключает скрипт вида
  `https://<id>.<hash>.eu-central-1.token.awswaf.com/<id>/<path>/challenge.js`.
- `challenge.js` (снятая копия 2026-09-03): 699 КБ, минифицирован в одну строку,
  обфусцирован, строковые литералы закодированы. Поверхность fingerprinting по
  вхождениям идентификаторов: `navigator` (34), `canvas` (15), `webgl` (5),
  `AudioContext` (1), `hardwareConcurrency` (5), `checksum` (29), `fetch` (7),
  `aws-waf-token` (12), `voucher` (4). WebAssembly не используется.
- Скрипт вычисляет решение (proof-of-work + согласованный набор browser signals) и
  обменивает его на токен через fetch к `*.token.awswaf.com`; страница затем
  перезагружается уже с cookie.
- Токен переносим между клиентами (minted в Chrome → принят в httpx), TTL ≈ 4 суток.
- Режим `x-amzn-waf-action: captcha` на сайте не наблюдался, но WAF его поддерживает —
  это отдельный стоп-сценарий (ниже).

## Два технических подхода

### A. Эмуляция JS-окружения (рекомендуемый)

Исполнять оригинальный `challenge.js` во встроенном JS-движке без браузера, подставляя
shim-слой браузерных API.

- Движок: `quickjs` (быстрый, ES2020+, легко шимить глобалы) или `mini_racer` (V8 isolate,
  ближе всего к Chrome-семантике `Error.stack`, нативных `toString` и прочих
  анти-tampering проверок). `js2py` отпадает: 700 КБ обфусцированного кода —
  непрактично медленно.
- Shim-bundle `BrowserEnv`: `window`, минимальный `document` (нужен
  `#challenge-container`, `createElement('canvas')`, cookie accessor),
  `navigator` (UA, languages, platform, hardwareConcurrency, deviceMemory,
  `webdriver: false`), `screen`, `performance`, `crypto.getRandomValues`/`subtle`
  (мост в `cryptography`), `fetch`/`setTimeout` (мост в httpx + asyncio event loop),
  детерминированные заглушки canvas 2D / WebGL / AudioContext.
- Плюсы: не зависит от внутренней логики AWS — переживает ротации алгоритма PoW и
  обфускации, пока стабилен набор запрашиваемых browser API.
- Минусы/риски: согласованность fingerprint (связка UA ↔ platform ↔ hardwareConcurrency ↔
  canvas/webgl-отклики должна выглядеть как реальный Chrome на Linux); анти-tampering
  проверки рантайма (нативность функций, стек-трейсы, порядок ключей объектов) — самая
  хрупкая часть; возможные серверные поведенческие эвристики (время решения PoW,
  тайминги между вызовами) — solver воспроизводит реалистичные задержки.

### B. Реимплементация протокола (реверс конкретной версии скрипта)

Расшифровать `gokuProps` (AES-GCM через WebCrypto → в Python `cryptography`),
воспроизвести PoW-поиск nonce и формат POST к token endpoint, не исполняя сам скрипт.

- Плюсы: максимально быстро и компактно, полный контроль, нет JS-движка.
- Минусы: AWS ротирует скрипт без предупреждения — solver ломается мгновенно и молча;
  требуется постоянный реверс свежих версий; по сути это целевой обход технической меры
  защиты, что юридически строже, чем «исполнение публичного скрипта как есть».

Решение: **A — основной путь**. B остаётся запасным исследованием, если эмуляция упрётся
в анти-tampering.

## Архитектура

```text
+---------------------------------------------------------------+
|                     AwsWafChallengeSolver                     |
|                                                               |
|  ChallengeFetcher   -- GET целевого URL -> 202-страница       |
|    - парсинг gokuProps + script URL                           |
|    - кеш challenge.js по content-hash                         |
|                                                               |
|  ScriptWatchdog     -- сверка content-hash с pinned версией   |
|    - новая версия -> canary-прогон ДО использования в scan    |
|                                                               |
|  JsRuntime (quickjs|mini_racer)                               |
|    + BrowserEnv shim-bundle (профиль "Chrome/Linux desktop")  |
|    + fetch-мост -> httpx (тот же SecureHttpClient allowlist)  |
|                                                               |
|  TokenExtractor     -- aws-waf-token из ответа/cookie-jar     |
+---------------------------------------------------------------+
            |
            v   aws-waf-token + expires
   WafTokenProvider (Redis store, single-flight, из rabota-md-http.md)
            |
            v
   WafHttpClient -> весь crawl plain HTTP
```

- Solver реализует тот же протокол `WafTokenProvider` backend'а (`get_token` /
  `invalidate`) и встраивается третьим backend'ом рядом с `EnvTokenProvider` и
  `StealthBrowserTokenMinter`. Цепочка приоритетов настраивается конфигом, например:
  `token_providers: [pure_python, env, stealth_browser]`.
- `ScriptWatchdog` пингует версию: `sha256(challenge.js)` хранится в Redis; новая версия
  не идёт в scan, пока не пройден canary-прогон (одиночный solve вне расписания) и не
  снят сигнал `solver_canary_ok`. Так ротация скрипта AWS превращается в алерт, а не в
  молча сломанный источник.
- Все сетевые вызовы solver'а (challenge page, challenge.js, token endpoint) проходят тот
  же rate limiter и доменный allowlist; `token.awswaf.com`/`captcha.awswaf.com`
  добавляются в allowlist только для solver'а, не для crawl-трафика.

## Жёсткие границы

- Решается **только** `x-amzn-waf-action: challenge`. При `captcha`, `block` или любом
  незнакомом action — немедленный `degraded`, источник в паузу, алерт оператору. Никаких
  попыток решать CAPTCHA: это зафиксированная граница всего проекта.
- Те же лимиты интенсивности, тот же идентифицирующий подход к UA, тот же гейт
  `policy_review_acknowledged` + `policy_review_reference`; включение solver'а — отдельное
  решение оператора с отдельным review reference.
- Solver живёт только внутри allowlist `rabota.md` и не является универсальным
  «обходчиком WAF»; fingerprint-профиль один, статический, без ротации личностей.
- Токен по-прежнему не логируется и не покидает Redis/память процесса.

## Риски и стоп-условия

- **Ротация challenge.js AWS** — штатное событие; смягчается ScriptWatchdog + canary +
  fallback-цепочкой провайдеров.
- **Усиление анти-tampering** (VM/движок-детекция, рантайм-проверки среды) — главный
  технический риск подхода A. Стоп-условие: если обход требует подделки свойств самого
  JS-движка, а не браузерных API, разработка останавливается, возврат к
  browser-минтеру.
- **Поведенческие эвристики сервера** (слишком быстрый PoW, нулевые тайминги) — solver
  обязан соблюдать реалистичные задержки; метрика `solve_duration_seconds` под
  наблюдением.
- **Правовая рамка** — исполнение публичного challenge-скрипта в собственном рантайме
  ближе к «самодельному браузеру», чем к взлому, но решение и ответственность — за
  оператором источника; документ не отменяет требований `docs/security.md` и
  политики доступа.
- **Техдолг** — shim-bundle требует сопровождения; оценка: spike 2–4 дня, production-grade
  1–2 недели плюс постоянное наблюдение за канарейкой.

## План внедрения

1. **Spike (изолированно, без интеграции):** standalone-скрипт вне production-кода:
   quickjs + минимальный `BrowserEnv` решает живой challenge rabota.md и получает
   рабочий `aws-waf-token` (проверка — один GET листинга). Критерий успеха: 3 успешных
   solve подряд с валидным crawl-ответом. Только после этого — интеграция.
2. **Интеграция:** backend `pure_python` в `WafTokenProvider`, ScriptWatchdog, canary-задача
   (1 solve/день вне scan), метрики `waf_solver_attempts_total`,
   `waf_solver_success_total`, `waf_solver_script_version`.
3. **Эксплуатация параллельно:** `pure_python` первым в цепочке, fallback на
   `stealth_browser` минтер. Наблюдение ≥ 4 недель: success rate ≥ 99%, ни одного
   необъяснимого `403/202` шторма.
4. **Удаление браузера:** после успешной фазы 3 `playwright`/`playwright-stealth`
   убираются из production-образа; `StealthBrowserTokenMinter` остаётся в коде под
   optional extra как аварийный инструмент разработчика.

## Проверки

- unit: fixtures записанной challenge-страницы и скрипта; парсинг `gokuProps`;
  корректность shim-ответов canvas/webgl; отказ при `captcha`-action; отсутствие токена
  в логах и метриках.
- canary: ежедневный solve вне scan с алертом при failure; смена content-hash скрипта без
  успешного canary блокирует использование solver'а в scan.
- integration: полный live smoke источника на `waf_http` с токеном от `pure_python`
  backend'а; сверка `content_hash` выдачи с browser-транспортом.
- деплой: стандартный production gate AGENTS.md; отдельный `policy_review_reference` на
  включение solver'а.
