# Rabota.md HTTP (waf_http) — резервный источник без Chromium

Дизайн-документ. Дата разведки: 2026-09-03. Статус: предложение, реализация не начата.

## Цель

Убрать тяжёлый stealth-Chromium (`playwright` + `playwright-stealth`) из горячего пути
crawling Rabota.md. Новый транспорт `waf_http` работает на plain HTTP (`httpx`) и
становится резервом текущего browser-адаптера, а после периода теневой эксплуатации —
основным. Chromium остаётся только как редкий, опциональный «минтер» WAF-токена, пока не
выбран окончательный способ его получения вне приложения.

## Результаты разведки live-сайта (2026-09-03)

Все проверки выполнялись read-only, без откликов на вакансии, с идентифицирующим
User-Agent `job-agent/0.1` либо браузерным UA. Выводы:

1. **Весь сайт за AWS WAF challenge.** Любой GET (`/`, `/ru/vacancies`, `robots.txt`,
   `sitemap.xml`, даже `api.rabota.md`) без валидной cookie возвращает
   `HTTP 202` + заголовок `x-amzn-waf-action: challenge` и стандартную challenge-страницу
   с `window.gokuProps`, которая грузит `challenge.js` с `*.token.awswaf.com`, вычисляет
   proof-of-work и ставит cookie `aws-waf-token`.
2. **TLS-фингерпринт не решает.** `curl_cffi` с impersonation `chrome120`/`chrome131`
   получает тот же 202 challenge. Гейт — только cookie, не отпечаток клиента.
3. **Токен переносим между клиентами.** Cookie, выпущенная в headless Chrome, принимается
   plain `httpx` с UA `job-agent/0.1` без какой-либо impersonation: listing, detail и AJAX
   пагинация отвечают `200` без WAF-заголовков. Привязки токена к TLS/UA не наблюдается;
   привязка к IP не проверялась и считается риском.
4. **TTL токена ≈ 4 суток.** Cookie `aws-waf-token` (domain `.www.rabota.md`, `Secure`,
   `SameSite=Lax`) выпущена 2026-09-03 21:47 UTC с `expires=2026-09-07 21:47 UTC`.
   Реальный immunity window сайт может уменьшить в любой момент.
5. **Все три нагрузки работают по plain HTTP с токеном:**
   - GET listing `/ru/vacancies`, `/ru/vacancies/category/<slug>` → `200`, карточки
     вакансии отрендерены на сервере (100 уникальных ID на странице категории `it`);
   - POST пагинации `/ru/vacancies/category/<slug>/<n>` → `200`,
     `{ "success": true, "data": { "content": "<html>" } }`, цепочка `data-next` цела;
   - GET detail `/ru/locuri-de-munca/<slug>/<id>` → `200`, присутствуют
     `.vacancy-content`, `h1.vacancy-title`, JSON-LD `JobPosting`, `mailto:`/`tel:`.
6. **POST требует браузерного набора заголовков.** Без них — `403 Forbidden` (не WAF):
   обязательны `X-Requested-With: XMLHttpRequest`, `Origin: https://www.rabota.md`,
   `Referer: <страница категории>`, `Accept: application/json, ...`,
   `Sec-Fetch-Dest: empty`, `Sec-Fetch-Mode: cors`, `Sec-Fetch-Site: same-origin`.
7. **Невалидный токен предсказуем:** запрос с bogus `aws-waf-token` снова получает
   `202` + `x-amzn-waf-action: challenge`. Это надёжный сигнал обновления токена.
8. **Headless Chrome без stealth не проходит.** Токен минтится, но перезагрузка страницы
   получает `403` — WAF оценивает браузер как автоматизацию. Поэтому минтер на старте
   переиспользует существующий `StealthPlaywrightBrowser`.

## Архитектура

```text
RabotaMdAdapter (без изменений: entrypoints, пагинация, нормализация, статусы)
        |
        |  HttpFetcher protocol: get(url) / post_html_fragment(url)
        v
+------------------+      202 challenge / 403       +----------------------+
|  WafHttpClient   | -----------------------------> |   WafTokenProvider   |
| (httpx + cookie) | <----------------------------- |  get / invalidate    |
+------------------+   aws-waf-token (single-flight)+----------------------+
                                                             |
                        +-------------------+  +-----------------------------+
                        | EnvTokenProvider  |  | StealthBrowserTokenMinter   |
                        | (аварийный, ops)  |  | (редкий запуск Chromium)    |
                        +-------------------+  +-----------------------------+
```

### WafHttpClient

Новая реализация существующего seam `HttpFetcher` поверх `SecureHttpClient`:

- один persistent cookie jar с `aws-waf-token`; токен подставляется только в запросы к
  `*.rabota.md`;
- `get(url)` — обычный GET; ответ `202` + `x-amzn-waf-action: challenge` трактуется как
  протухший токен: invalidate → single-flight refresh → один retry. Повторный challenge
  после refresh — `RabotaMdDegradedError`, как сейчас;
- `post_html_fragment(url)` — POST с фиксированным браузерным набором заголовков из
  п. 6 разведки (Referer вычисляется из URL страницы 1 категории); контракт ответа
  (`success=true`, строковый `data.content`) совпадает с браузерной реализацией, поэтому
  код адаптера не меняется;
- rate limit, allowlist доменов, SSRF-валидация URL и redirect-проверки — те же, что у
  `SecureHttpClient` (50 rpm, интервал ≥ 1.2 с, только `rabota.md`/`www.rabota.md`);
- `token.awswaf.com`/`captcha.awswaf.com` из allowlist браузера HTTP-транспорту не нужны.

### WafTokenProvider

Протокол `async get_token() -> str` / `async invalidate()`. Хранилище — Redis
(уже является зависимостью): ключ `crawler:rabota_md:waf_token` с TTL из `expires` cookie
минус защитный запас (предлагается 12 часов). Обновление — single-flight через Redis lock,
чтобы параллельные ветки scan не минтили токен одновременно. Токен — чувствительное
значение: не логируется, не попадает в audit/observability, не коммитится.

Backends (в порядке приоритета):

1. **`StealthBrowserTokenMinter`** — стартовый вариант. Поднимает существующий
   `StealthPlaywrightBrowser` на одну навигацию `/ru/vacancies`, ждёт разрешения
   challenge (механика уже реализована в `StealthPlaywrightBrowser.get`), забирает cookie
   и закрывает браузер. Запускается максимум раз в TTL токена (≈ раз в 3–4 суток) плюс по
   сигналу invalidate.
2. **`EnvTokenProvider`** — аварийный ручной канал: токен из переменной окружения/
   secret-файла, выпущенный оператором из обычного браузера. Позволяет пережить поломку
   минтера без деплоя.
3. **Pure-Python решатель challenge.js** — вынесен в отдельный дизайн
   [rabota-md-pure-python.md](rabota-md-pure-python.md): исполнение оригинального скрипта
   во встроенном JS-движке с shim-слоем браузерных API. Хрупкость и правовая рамка
   описаны там; до завершения его spike основными остаются backends 1–2.

Полное удаление Chromium из зависимостей становится возможным, когда минтер вынесен за
пределы приложения (отдельная ops-процедура/внешний сервис) либо сайт смягчил WAF.
До этого момента `playwright` extra остаётся, но перестаёт быть нужным для самого crawl.

### Изменения в адаптере и конфиге

`RabotaMdAdapter` уже принимает `HttpFetcher` и содержит весь parsing — его код не
меняется. Меняется только выбор транспорта по конфигу:

```yaml
source:
  id: rabota_md
  adapter: rabota_md
  transport: waf_http          # stealth_browser | waf_http | auto
  # ... остальные ключи без изменений
```

- `stealth_browser` — текущее поведение, без изменений;
- `waf_http` — только `WafHttpClient`; если минтер недоступен и токена нет, источник
  стартует в `degraded`, а не падает молча;
- `auto` — резервный режим на переходный период: основной `stealth_browser`, при
  `BrowserFallbackUnavailable` или degraded-сигнале переключение на `waf_http` в рамках
  того же scan с сохранением checkpoint.

### Границы доверия и безопасность

- Те же fail-closed правила: 403/429/5xx и challenge после retry — degraded/temporary
  ошибки, никогда «пустая выдача»; массовые переходы статусов при деградации отключены.
- Токен не расширяет права: он лишь воспроизводит сессию обычного посетителя публичных
  страниц. Allowlist публичных путей (`_allowed_public_path`) остаётся без изменений,
  внутренние endpoints (`/ajax/`, `/cabinet/`, …) по-прежнему запрещены.
- Политика доступа не меняется: `policy_review_acknowledged` + `policy_review_reference`
  обязательны, живой трафик — умеренной интенсивности с теми же лимитами.
- HTML по-прежнему недоверенный ввод; parsing на selectolax и все нормализационные
  правила (контакты только из vacancy-owned блоков и т.д.) общие для обоих транспортов.

### Отличия от browser-транспорта, которые нужно держать под наблюдением

- Browser-транспорт отдаёт rendered DOM, HTTP — серверный HTML. На дату разведки карточки
  листинга, `.vacancy-content`, JSON-LD и контакты рендерятся на сервере; unit fixtures
  общие, но live smoke обязан сверять оба транспорта на одних и тех же вакансиях.
- Возможная скрытая привязка токена к IP/фингерпринту не наблюдалась, но сайт может её
  включить: метрика «доля 202/403 по транспорту» — обязательный сигнал регресса.
- POST-контракт пагинации (заголовки, форма JSON) — внутренний контракт сайта; его смена
  ломает оба транспорта одинаково, детектится существующими parse-error метриками.

## План миграции

1. **Фаза 1 — реализация:** `WafHttpClient`, `WafTokenProvider` (Redis store,
   single-flight), `StealthBrowserTokenMinter`, `EnvTokenProvider`, ключ `transport` в
   конфиге. Unit-тесты на существующих fixtures через injected fetcher; интеграционный
   live smoke ограниченного числа вакансий без откликов.
2. **Фаза 2 — резерв:** `transport: auto` на проде после стандартного deployment gate;
   `waf_http` срабатывает при деградации браузера. Сравнение выдачи транспортов по
   `content_hash` на одних ID.
3. **Фаза 3 — основной:** после ≥ 2 недель стабильных метрик `waf_http` становится
   транспортом по умолчанию; Chromium работает только как минтер.
4. **Фаза 4 — решение об удалении:** вынос минтинга из приложения (ops-процедура с
   `EnvTokenProvider`) либо сохранение минимального browser-extra. Только после этого
   `playwright`/`playwright-stealth` исключаются из production-образа.

## Проверки

- unit: парity parsing одних и тех же fixture HTML обоими транспортами; обновление токена
  по 202; 403 на POST до retry-политики; single-flight refresh; отсутствие токена в логах.
- integration: live smoke `waf_http` — listing, 2+ страницы AJAX-пагинации, detail,
  recheck; сверка `content_hash` с browser-транспортом на пересекающихся ID.
- observability: метрики `rabota_md_waf_token_refresh_total`,
  `rabota_md_waf_challenge_total`, доля degraded по транспортам.
- деплой: только через стандартный production gate AGENTS.md (локальная валидация,
  e2e, явная авторизация оператора).
