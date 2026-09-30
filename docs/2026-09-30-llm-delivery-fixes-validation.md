# Проверка исправлений LLM-контракта и доставки (handoff 2026-09-30)

Исправления по [handoff](handoff.md) и [исследованию](2026-09-30-llm-delivery-investigation.md)
разработаны и проверены локально. PROD, PhoneGate и production llmRouter
не изменялись; `/srv/llmrouter` использовался только для чтения и клонирования.

## Коммиты

JobHunter, ветка `fix/llm-delivery-handoff` поверх `investigate/llm-delivery-september-30`
(включает ещё не развёрнутый `45ed3b6` с панелью и Gmail consent):

| Коммит | Пункт | Суть |
| --- | --- | --- |
| `36cc3ea` | 1 | Журнал передач `email_send_attempts` для hard maximum; миграция `a3e9c2d7b4f1` с backfill |
| `18ff6a1` | 2 | DNS preflight почтовой маршрутизации, распространение отказа на домен |
| `d179913` | 3 | Отчёт берёт привязанную оценку; снимок `send_authorization` |
| `bfad41a` | 4, 5 | Переносимая strict-схема, ограниченный repair/retry, `llm_*` поля оценки (миграция `b7f1e4c9a2d3`), классификация Router в телеметрии |
| `fbe88d8` | 6 | Явный контракт MCP `get_daily_report`, документация счётчиков |

llmRouter: DEV-клон `/home/andrei/llmrouter-dev`, ветка `fix/structured-contract-trace`,
коммит `a3124b1` поверх production `59f3f7a`.

## Что изменилось по пунктам

1. **Hard maximum.** Отправщик резервирует строку `in_flight` под существующей
   дневной advisory-блокировкой до вызова провайдера и записывает итог
   (`provider_accepted`, `delivery_unknown`, `not_transmitted`). Policy,
   планировщик и отчёт считают один журнал по локальному дню фактической попытки.
   Bounce меняет только `EmailDelivery`, поэтому слот не освобождается. Минимум
   считается отдельно.
2. **DNS preflight.** NXDOMAIN, null MX и отсутствие MX и A/AAAA блокируют отклик
   без вызова провайдера и помечают `rejected` все контакты домена. Timeout,
   SERVFAIL и недоступный resolver только откладывают попытку. DSN
   `domain_not_found`/`routing_failure` тоже распространяется на домен.
   Классификатор распознаёт `Host or domain name not found` / `5.1.2`.
3. **Исторический score.** `sent_applications[].overall_score` — оценка
   `Application.match_evaluation_id`. Без привязки score равен `null` с пометкой
   `unknown`; последняя переоценка выводится отдельно. Каждая попытка сохраняет
   снимок разрешения отправки.
4. **Strict-контракт.** Все поля обязательны, `additionalProperties=false` на
   всех уровнях, `$ref` раскрыт, ограничения вынесены в описания. Локальная
   Pydantic-валидация не ослаблена.
   Router сохраняет в trace `upstream_status`, `router_synthetic` и
   `failure_class` и на час отправляет в карантин провайдер/модель после двух
   отказов контракта structured output. Обычные запросы карантин не затрагивает.
5. **Repair и телеметрия.** Невалидный ответ повторяется только с изменённым
   запросом: подсказкой с путём и типом ошибки (без самого значения) или, после
   `length`, одним увеличенным бюджетом до 3072 токенов. Детерминированный 400
   не повторяется. После исчерпания попыток остаётся безопасный review-fallback.
   В оценке сохраняются `llm_logical_request_id`, `llm_outcome`, код, путь и
   число попыток. Восстановление транспорта и валидность схемы считаются
   раздельно.
6. **MCP.** Отчёт живой и только для чтения, в одной транзакции `REPEATABLE READ`.
   Добавлены `scopes`, время сбора, `counter_definitions`,
   `provider_submissions` / `initially_accepted` /
   `submitted_cohort_current_status` и счётчики `llm_evaluation_outcomes`.

## Проверки

JobHunter (Python 3.12, hermetic env: mock LLM, fake email, DNS preflight off
по умолчанию, без live crawl/GSM/PhoneGate/Gmail):

- `ruff check .`, `ruff format --check .`: успешно; `mypy app fixture_site`:
  успешно, 169 файлов.
- Полный `pytest`: **1486 passed, 22 skipped**, 906,63 с (baseline handoff —
  1450/21). Новые тесты: `test_daily_hard_maximum.py`, `test_mail_routing.py`,
  `test_llm_contract.py`, `test_mcp_report_contract.py`, дополнения в
  reconciliation/reports/migrations/concurrency. Пропущены opt-in PostgreSQL/Redis,
  browser, live crawler/PhoneGate и real GSM проверки — они прогнаны отдельно
  там, где касаются изменений (ниже).
- Одноразовый PostgreSQL 16 (контейнер `jh-dev-test-pg`, удалён после проверки):
  `alembic upgrade head` → `downgrade d830a4b26f19` → `upgrade head` и
  `alembic check` без расхождений; concurrency-тесты минимума, двух параллельных
  отправителей при максимуме 1 и слота работодателя — 5 passed.
- SQLite-миграция: backfill журнала (accepted→bounced, temporary, unknown,
  sending) по локальному дню и сохранение доставок при downgrade.
- `scripts.verify_daily_minimum_e2e` (fake Gmail/LLM, локальный сервер, чистые
  Playwright Chromium contexts): три запуска подряд, в каждом PASS 1/3–3/3.
- Реальный DNS (ручная read-only проверка, не тест): `unitesto.md` → `no_domain`
  (NXDOMAIN, домен инцидента), `example.com` → `null_mx`, `gmail.com` → `routable`.

E2E через оба сервиса (локальный Router-dev на 127.0.0.1:47400 с отдельным HOME,
заглушки groq/cerebras на 127.0.0.1:47401; groq-заглушка отвергает strict-схему,
если `required` ≠ `properties`, cerebras отдаёт обрезанный ответ):

| Сценарий | Результат |
| --- | --- |
| Старый контракт, запросы 1–2 | trace `groq 502←400 structured_output_rejected`, `cerebras 502←200 truncated`; после исчерпания — prompt JSON |
| Старый контракт, запрос 3 | groq в карантине structured-output: 5 вызовов groq вместо 6 |
| Новый контракт, чистый Router | groq-заглушка принимает strict-схему с первой попытки, один upstream вызов |

llmRouter (`/home/andrei/llmrouter-dev`, собственный venv): `tests/unit/test_router.py`
81 passed; полный набор 781 passed, 3 failed. Эти 3 падения (`test_app_mounts_embeddings_route`,
`test_images_routes_registered`, `test_img2img_uploads_reference_and_returns_bytes`)
воспроизводятся и на исходном `59f3f7a` в этом venv (версии FastAPI/imager), к
изменениям не относятся; `ruff check` и `mypy src` успешны.

## План выкладки (требует отдельного разрешения оператора)

JobHunter:

1. Слить `fix/llm-delivery-handoff` в канонический DEV `main` (fast-forward:
   ветка линейно продолжает `40de08e`). Оператор должен учесть, что вместе с
   этой веткой уходят `45ed3b6` (панель, Gmail consent) и docs-коммиты.
2. Синхронизировать PROD через `deploy/sync-prod-code.sh`: только
   fast-forward к точному DEV `main`. Собрать образ через `sudo systemd-run`.
3. На время миграции остановить отправку: поставить `global_pause` или
   остановить `beat` и `control-worker`. Старый код не пишет журнал, и передача
   в окне между backfill и перезапуском не попала бы в максимум.
4. `alembic upgrade head` ролью migrator: `a3e9c2d7b4f1` (новая таблица и
   backfill по последней попытке каждой доставки) и `b7f1e4c9a2d3` (nullable
   колонки). Обе миграции только добавляют.
5. Пересоздать все сервисы на одном digest образа, снять паузу, выполнить
   read-only проверки: health; `get_daily_report` показывает
   `email_delivery.provider_submissions` и `scopes`; внутри control-worker
   `default_mail_routing_checker(...).check_domain("gmail.com")` возвращает
   `routable`, то есть контейнер резолвит MX.
6. Гигиена Docker по `AGENTS.md`: digest, `docker system df`, `df -h /`, удалить
   устаревшие образы JobHunter, кроме одного образа для отката.

llmRouter (отдельный сервис, отдельное разрешение): fast-forward
`/srv/llmrouter` к `a3124b1` и перезапуск по его runbook. Порядок выкладки
относительно JobHunter не важен: JobHunter читает новые поля trace, если они
есть. Перед выкладкой Router стоит устранить три unit-падения, существующие
и до изменений (подробнее — в разделе проверок).

## Откат

- JobHunter: вернуть предыдущий образ на всех сервисах. Миграции только
  добавляют, поэтому старый код игнорирует новую таблицу и колонки. Downgrade
  (`alembic downgrade d830a4b26f19`) удаляет журнал и `llm_*` поля — делать его
  только если это нужно.
- DNS preflight отключается без отката кода: `MAIL_ROUTING_PREFLIGHT_ENABLED=false`
  и перезапуск worker-ов.
- llmRouter: вернуть checkout на `59f3f7a` и перезапустить; карантин хранится
  в памяти и исчезает при перезапуске.
