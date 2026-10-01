# Проверка исправлений LLM-контракта и доставки (handoff 2026-09-30)

Исправления по [handoff](handoff.md) и [исследованию](2026-09-30-llm-delivery-investigation.md)
разработаны и проверены локально, затем 2026-10-01 развёрнуты в PROD по
разрешению оператора (см. «Выкладка 2026-10-01»). PhoneGate не изменялся.

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

## Выкладка 2026-10-01

Оператор разрешил слияние DEV-веток и выкладку в текущем разговоре.

- Git: `fix/llm-delivery-handoff` влита в DEV `main` fast-forward (`40de08e` →
  `8167300`); `fix/panel-controls-gmail` (`45ed3b6`) входит в неё. Локальные
  ветки, чьё содержимое уже было в `main`, и лишние worktree удалены; остался
  один checkout на `main`. На GitHub не пушилось.
- Перед выкладкой панель из `fix/panel-controls-gmail` просмотрена на локальном
  сервере в Playwright Chromium (19 экранов, desktop и 390 px): выбор профиля,
  блок и диалог автоотправки, «Пользователи», sidebar, `/app`; ошибок консоли и
  горизонтального overflow нет. Блёклые кадры при первом снимке — анимация
  появления (`dialog-in`, 0,16 с), подтверждено замером opacity.
- PROD: `deploy/sync-prod-code.sh` → `8167300`; образ
  `jobhunter-prod:8167300ae75d` собран через `systemd-run`.
- Backup `job-agent-job_agent-current.dump` (215 МБ), checksum OK; тестовое
  восстановление в изолированный контейнер без сети: 659 доставок / 8808 откликов
  / 103491 оценка — как в PROD. На этой копии отрепетированы обе миграции:
  журнал 659/659, за 30.09 — 21 передача (старый счётчик давал 20), за 01.10 — 9.
- `beat` остановлен, дождались простоя четырёх worker (в отправке и звонках
  ничего не было), worker остановлены, `alembic upgrade head` ролью migrator
  (`d830a4b26f19` → `b7f1e4c9a2d3`), затем все семь сервисов подняты на одном
  образе. Простой фоновой обработки — около 5 минут (09:03–09:08 EEST).
- После выкладки: семь контейнеров на digest `8d9b12e062de`, `APP_REVISION`
  совпадает; публичные `/health` и `/ready` — 200; четыре Celery pong; в логах
  нет ошибок, периодические задачи завершаются успешно; роль `jobhunter_app`
  имеет права на `email_send_attempts`; живой отчёт (тот же код, что MCP):
  `daily_limit_used=9` из журнала, score отправок — `bound_evaluation`;
  страницы панели на PROD-данных отдают 200, счётчик «9 из 20» одинаков на всех,
  включая «Пользователи»; DNS preflight внутри `control-worker`: `gmail.com` →
  `routable`, `unitesto.md` → `no_domain`.
- llmRouter: fast-forward `/srv/llmrouter` `59f3f7a` → `a3124b1`, перезапуск
  `llmrouter.service`; `healthz` ok, десять провайдеров доступны. С версиями
  FastAPI/Starlette как в PROD полный набор Router — 783 passed, 1 failed
  (тест imager требует PIL, к изменению не относится).
- Три контрольных вызова реального провайдера из `matching-worker` с синтетической
  вакансией (без записи в БД): Groq `openai/gpt-oss-20b` и `openai/gpt-oss-120b`
  приняли strict-схему с первой попытки; третий вызов — настоящий Google 429 с
  новыми полями trace и успешный fallback. Все три — `schema_valid=true`.
- Docker: build cache и неиспользуемый образ утилиты backup удалены; `/` — 70 %.
  Предыдущий образ `jobhunter-prod:4f04db049505` оставлен на окно отката.

## План выкладки (исполнен 2026-10-01)

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
