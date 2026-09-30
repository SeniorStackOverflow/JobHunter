# Handoff для Claude: исправления после исследований JobHunter

Дата передачи: 2026-09-30. Оператор поручил реализацию исправлений Claude.
Этот документ передаёт результаты исследований и критерии приёмки; сами
перечисленные ниже исправления LLM/доставки ещё не реализованы.

## Что прочитать сначала

| Материал | Для чего нужен |
| --- | --- |
| [Исследование LLM, score 0, bounce и sent/submitted](2026-09-30-llm-delivery-investigation.md) | Основное расследование: точный исторический срез, причины ошибок, проверка MCP, подтверждённые дефекты и ограничения доказательств |
| [Read-only SQL к исследованию](2026-09-30-llm-delivery-evidence.sql) | Семь запросов для сверки исторической когорты; mutable статусы не восстанавливают произвольный прошлый snapshot |
| [Исходный диалог ChatGPT](https://chatgpt.com/share/6abd5452-9a04-83ed-b731-2c78b0c582f9) | Источник пользовательской сводки; опубликованный текст не содержит полного JSON-ответа MCP и трассы вызова |
| [Исследование недобора минимума 27–28 сентября](2026-09-29-daily-minimum-investigation.md) | Предыдущая диагностика и исходные требования; читать вместе с результатами реализации |
| [Проверка реализации и rollout добора](2026-09-30-daily-minimum-validation.md) | Что уже реализовано и развёрнуто, проверка конкурентности, миграции и границы исторического replay |
| [Исторический replay минимума](2026-09-30-daily-minimum-replay.json) | Обезличенные агрегаты; не доказательство доступности новых безопасных кандидатов в прошлом |
| [Проверка панели и Gmail consent](2026-09-30-panel-controls-validation.md) | Уже сделанные локальные исправления и браузерные проверки; этот коммит ещё не развёрнут |
| [AGENTS.md](../AGENTS.md), [окружения](environments.md), [deployment runbook](deployment.md) | Обязательные ограничения разработки, тестов, синхронизации DEV/PROD и выкладки |

Рабочие правила: [автоотправка](auto-send-policy.md),
[добор минимума](daily-minimum-catchup.md),
[отношения с работодателями](employer-relationships.md), [MCP](mcp.md).
Исторические выводы датированы; текущий код и состояние окружений перепроверить
перед реализацией. Полные свидетельства и оговорки находятся в исследованиях.

## Состояние передачи и существующие изменения

Проверено в локальном checkout перед созданием handoff:

- Рабочая ветка: `investigate/llm-delivery-september-30`; исходный HEAD — `56bb9e3`.
- Канонический DEV `refs/heads/main` — `40de08e`; рабочее дерево было чистым.
- `45ed3b6`: локальные исправления панели, счётчиков пользователей и отдельного
  Gmail consent. Они уже входят в текущую ветку; не реализовывать повторно.
- `83c8aed`: исследование LLM/доставки и SQL; `56bb9e3`: сверка MCP и исходного
  диалога. Изменений runtime в этих двух коммитах нет.
- По read-only проверке, зафиксированной в исследовании: PROD checkout
  `40de08e`, приложение `jobhunter-prod:4f04db049505`, llmRouter checkout
  `59f3f7a`. Это состояние на момент исследования, а не новая проверка PROD.
- Добор минимума `4f04db0` уже развёрнут. Старый план недобора не является
  списком целиком невыполненных задач; новый дефект hard maximum остаётся открытым.

Не сбрасывать текущую ветку на main и не терять `45ed3b6`.
Выбор ветки реализации сделать после `git status` и просмотра diff.
Для синхронизации PROD использовать именно канонический DEV main, а не feature HEAD.

## Подтверждённые факты, которые нужно сохранить

- Исходный срез: 1289 provider attempts, 408 HTTP-success, 881 failed attempts;
  393 logical requests, 368 affected/recovered, 0 transport-level final failures.
  Отдельно: 11 невалидных matching-результатов — 9 `literal_error`, 2 `length`.
  ChatGPT в исходной сводке различал эти показатели. Свежая сверка MCP с БД
  совпала; неисправность MCP-транспорта не установлена.
- 718 Groq «502» были upstream 400, ещё 19 «502» — обрезанными HTTP 200.
  Причина несовместимости исходящей strict-схемы подтверждена локально, но тела
  старых 400 не сохранены: нельзя приписывать всем 718 один доказанный текст отказа.
- Предполагаемая отправка со score 0 на самом деле привязана к оценке 80.
  Отчёт подставляет более позднюю переоценку. Все 21 отправки когорты привязаны
  к валидным оценкам 80–98; обход send policy не подтверждён.
- `21 submitted = 20 current provider_accepted + 1 domain_rejected`.
  Это согласованный ledger. Provider acceptance не доказывает доставку в ящик.
  Отдельно доказан дефект лимита: bounce освободил слот, и при максимуме 20
  провайдеру передали 21 письмо.
- Отказ домена `unitesto.md` подтверждён DSN и DNS; синтаксическая проверка
  контакта не проверяет mail routing. Permanent bounce не запланирован на retry.

## Порядок исправлений и критерии приёмки

### 1. Hard maximum: bounce не должен возвращать израсходованный слот

Точки входа: [PolicyEngine](../app/policies/engine.py),
[sender](../app/email/service.py), [delivery lifecycle](../app/email/delivery.py),
[метрики отчёта](../app/reports/service.py).
Сейчас `attempts_today` использует дату создания delivery и текущие статусы.
После `domain_rejected` ранее принятое письмо исчезает из лимита.

Считать расход hard maximum по устойчивому факту передачи/принятия и
неопределённости, плюс in-flight резерв под существующей сериализацией.
Учесть локальные границы дня, фактическое время передачи, повторные попытки и
идемпотентность. Конкретную модель ledger выбрать после чтения lifecycle;
не подменять её простым подсчётом всех `created_at` или всех failed.
Минимум успешных отправок и максимум передач должны иметь разные счётчики.

Приёмка: accepted → permanent bounce не освобождает максимум; подтверждённый
отказ до принятия обрабатывается по политике; unknown сохраняет резерв;
параллельные workers не превышают максимум; retry на другом локальном дне
учитывается по фактической передаче. Сохранить employer slot и идемпотентность.
Проверить настоящий PostgreSQL concurrency и локальный pipeline E2E с fake sender.
Тестовые точки: [policy/email](../tests/unit/test_policy_and_email.py),
[delivery reconciliation](../tests/unit/test_email_delivery_reconciliation.py),
[concurrency](../tests/integration/test_daily_minimum_concurrency.py).

### 2. DNS preflight перед provider submission

Точки входа: [контакты](../app/contacts/service.py), sender и delivery lifecycle.
Сохранить синтаксический normalizer; добавить отдельную проверку mail routing
с ограниченным временем, кешем и TTL. NXDOMAIN, null MX и отсутствие mail
routing блокируют отправку. Timeout/SERVFAIL/недоступный resolver откладывают
попытку и не уничтожают контакт как постоянный отказ. Учесть допустимый A/AAAA
fallback при отсутствии MX. DNS не подтверждает существование ящика.
Доменный отказ распространять на другие контакты того же домена.

Приёмка: fake NXDOMAIN/null MX/no routing не вызывают provider; валидный routing
пропускает дальнейшие policy checks; временная DNS-ошибка остаётся retryable;
второй контакт мёртвого домена блокируется. Резолвер внедрить зависимостью:
тесты offline, без настоящих DNS/email calls.
Тестовые точки: [contact discovery](../tests/unit/test_contact_discovery.py),
policy/email и delivery reconciliation.

### 3. Исторический score и доказательство разрешения отправки

Точка входа: `app/reports/service.py::_generate` — сейчас выбирает последнюю
оценку `(profile, canonical_job)` для `sent_applications[].overall_score`.
Использовать `Application.match_evaluation_id`; при отсутствии привязки явно
показать неизвестную оценку. Последнюю переоценку, если нужна, показывать отдельно.
Сохранить в send snapshot ID оценки, score, effective threshold и fingerprint
настроек для новых отправок; не выдумывать отсутствующие исторические значения.

Приёмка: bound score 80 → новая invalid score 0 → новая valid score 80/skip
не меняют оценку исторической отправки; detail и MCP согласованы с bound evaluation.
Тестовые точки: [reports](../tests/unit/test_reports.py), policy/email.

### 4. Совместимая исходящая strict-схема и классы ошибок Router

Точки входа JobHunter: [MatchResult](../app/matching/schemas.py),
[`LLMRouterProvider._body`](../app/matching/providers.py).
`strict=true` отправляется с двумя default-полями, отсутствующими в `required`.
Подготовить совместимую исходящую JSON Schema, включая вложенные объекты,
отдельно от локальной модели/совместимости сохранённых оценок.
Не ослаблять Literal, hard requirements, scam и material-risk проверки.

llmRouter — отдельный репозиторий. В исследовании указан production путь
`/srv/llmrouter/src/llmrouter/routing/router.py`, `Router.execute` и
`_attempt_error_trace`; это указатель для диагностики, не место разработки.
Изменения Router готовить в локальном или non-production checkout:
сохранять upstream status и synthetic marker, учитывать повторные contract/
capability failures, не повторять детерминированный 400 как обычный 5xx.

Приёмка: stub provider проверяет реально исходящее тело и strict contract;
400 → fallback сохраняет классификацию; truncated 200 отличается от настоящего
upstream 502; несовместимый кандидат не создаёт бесконечную цепочку повторов.
Тестовые точки JobHunter: [matching](../tests/unit/test_matching.py),
[telemetry](../tests/unit/test_telemetry.py); Router — его собственные тесты
и локальный E2E через оба сервиса. Не обращаться к платным/live LLM в тестах.

### 5. Application-level telemetry и ограниченный repair/retry

Точки входа: providers, [matching service](../app/matching/service.py),
[telemetry](../app/telemetry.py). Добавить устойчивую связь оценки с logical request
и безопасные тип/путь ошибки без сырого ответа, резюме или ошибочного input value.
После invalid output дать ограниченную обратную связь/выбрать совместимую модель;
после length пересмотреть ограниченный бюджет. Не повторять неизменный запрос
к той же модели без причины. Сохранить безопасный `_safe_fallback`.

Приёмка: transport recovered и schema validated считаются отдельно;
literal/length после исчерпания бюджета дают review/material risk и никогда
не разрешают auto-send; retry имеет явную верхнюю границу и наблюдаемую причину.
Корреляцию по ближайшему времени не выдавать за сохранённый FK.
Тестовые точки: matching, telemetry, policy/email.

### 6. Контракт MCP и документация счётчиков

Точки входа: [`get_daily_report`](../app/mcp/server.py), reports, telemetry.
Разделить attempts/logical requests, transport recovery/schema validation,
upstream/synthetic status, initial acceptance/current delivery outcome.
Сохранять scope и времена сбора; решить и описать согласованность snapshot.
Tool description должен соответствовать текущей сборке live report, а не
обещать выдачу последнего сохранённого отчёта. Сохранить read-only поведение.

Приёмка: deterministic fixture через MCP отражает тот же ledger и bound score,
что прямой отчёт; названия и описание объясняют 20/21 без потери bounce;
разные scopes не представлены как одна когорта; вызов не пишет в БД и не запускает
matching, crawler или отправку. Обновить MCP, auto-send-policy и daily-minimum-catchup:
последний сейчас ошибочно обещает сохранить статус SENT после bounce;
код сохраняет `sent_at`, но переводит Application в FAILED.
Тестовые точки: reports, telemetry и тесты MCP после выбора текущего harness.

## Проверки и граница выкладки

Предыдущий baseline из исследования: Python 3.12, Ruff check/format, mypy по
167 файлам; pytest **1450 passed, 21 skipped**. Эти проверки подтверждают базу,
а не будущие исправления. Полные проверки новой реализации обязательны:

```bash
ruff check .
ruff format --check .
mypy app fixture_site
pytest
```

Запускать в изолированном test-окружении: mock LLM, fake email, без загрузки
developer `.env`, live crawling, real email/GSM/PhoneGate и настоящих секретов.
Opt-in PostgreSQL/Redis и браузерные проверки запускать отдельно локально,
когда они нужны для изменённого поведения; skipped не считать доказательством.
Перед production нужен успешный E2E именно изменённого поведения; для browser,
auth/OAuth и других integration-sensitive изменений — Playwright не менее трёх
раз подряд с чистым контекстом. Если меняется схема — проверить upgrade/downgrade
и сохранение существующих данных в изолированной БД.

Разработка только в локальном/non-production checkout. Не менять PROD, PhoneGate
или их секреты. После реализации: focused проверки, project checks, E2E,
объясняющий commit и конкретный rollout/rollback plan оператору. Выкладка требует
нового явного разрешения по AGENTS.md; handoff такого разрешения не даёт.
Синхронизация PROD main — только fast-forward к точному каноническому DEV main.
После разрешённой выкладки проверить health, единый application image digest,
диск и очистку только obsolete JobHunter images/cache по runbook.

Результат работы Claude: исправленные открытые пункты с регрессиями и локальным
E2E, обновлённая документация контрактов, список коммитов и проверок, отдельные
изменения/проверки llmRouter при необходимости и план безопасной выкладки/отката.

## Проверка самого handoff

При подготовке этого документа повторно прошли `ruff check .`,
`ruff format --check .`, `mypy app fixture_site` (167 файлов) и полный
безопасный `pytest`: **1450 passed, 21 skipped**, 713,39 секунды, exit code 0.
JUnit: `/tmp/jobhunter-handoff-validation.xml` — временный локальный артефакт,
не включённый в репозиторий. Пропущены 9 PostgreSQL/Redis, 2 opt-in browser,
2 live crawler/PhoneGate и 8 real GSM проверок. Все 30 локальных ссылок handoff
существуют; ссылка на него добавлена в индекс документации.
Runtime и production в рамках подготовки handoff не менялись.
