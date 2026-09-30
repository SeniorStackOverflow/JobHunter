# Проверка реализации добора минимума

Дата: 2026-09-30. Реализация начата от канонического DEV main `cc7a77a`,
совпадающего с checkout PROD. До отдельной выкатки runtime PROD остаётся `f4df0e2`.
Изменения разрабатывались в `feat/daily-minimum-catchup`; PROD не изменялся.

## Соответствие исследованию

| Требование | Реализация | Проверяемое свидетельство |
| --- | --- | --- |
| 1. Read-only воронка и итог за вчера | `daily-minimum-audit`, исключающие primary blockers, отдельные secondary rules, неизменяемый finalized report | `test_minimum_audit_has_exclusive_funnel_and_does_not_mutate`, `test_finished_report_is_previous_day_and_immutable`; [исторические агрегаты](2026-09-30-daily-minimum-replay.json) |
| 2. Ступени 60/50/40, мягкие review/skip | Явные `soft_mismatches`, отдельные необязательные требования, не более одного отсутствующего преимущества; минимальное необходимое ослабление | `test_catchup_stages_use_least_required_relaxation`, `test_aggressive_catchup_accepts_at_most_one_explicit_optional_gap`, `test_replay_can_include_soft_skips_but_never_owner_rejections` |
| 3. Поиск и подготовка по цели | Актуальный дневной резерв; разные свободные компании первыми; подготовка каждые 5 минут; incremental поиск каждый час/15 минут в конце дня | Unit tests резерва, отмены, паузы, дня, постоянного отказа доставки, выбранных источников и частоты поиска; pipeline E2E показывает `remaining: 1 → 0` и прекращение добора |
| 4. Один активный отклик в компанию | Фиксированный единичный employer slot и консервативная проверка названия при разных ID; финальная проверка перед provider call | PostgreSQL `test_parallel_promotions_reserve_only_missing_distinct_companies` в двух вариантах; существующий `test_two_workers_competing_for_one_employer_slot_only_authorize_one`; fake outbox с разными получателями |
| 5. Старый безответный контакт | Отдельный авторизованный API, свежая синхронизация Gmail, окно ожидания, долгий cooldown, append-only event/audit | `test_unanswered_closure_is_explicit_and_keeps_frequency_cooldown` со свежим контактом, ответом, неизвестной доставкой, отсутствующим cursor и поздно найденным ответом; локальный E2E реального API |
| 6. Объяснение deferred | Независимые поля safety/content/slot/soft fit, разделение employer-only и смешанных отказов в отчёте/панели | Policy tests, tests интерфейсов и detail mapping; готовый резерв исключается из дополнительных работодателей replay |

`tests/unit/test_daily_minimum.py` также проверяет обязательное условие,
материальный риск, scam и неклассифицированный skip: ни один не разрешает
агрессивную отправку. После отказа/отмены может рассматриваться другая компания,
но явное отклонение владельца не отменяется. Историческая отправка не переписывается.

## Границы исторического воспроизведения

Read-only выборка PROD подтверждает одну отправку 27 сентября и одну 28 сентября;
при цели 2/20 — недобор 1 в день. По текущим сохранённым проверкам исторических
когорт нет дополнительных готовых работодателей ни на 60, ни на 50, ни на 40.
Исследование уже объясняет отсутствие безопасной очереди и свободный лимит 19.

Это не восстановление всех исторических решений: прежние policy snapshots
каждого момента и новая классификация soft skip отсутствуют. Приписывать старым
оценкам новые мягкие причины нельзя. Сценарии мягкого review/skip и минимального
ослабления поэтому воспроизводятся отдельно на подтверждённых fixture-входах
без Gmail и crawling. CLI явно указывает scope сохранённой политики и время
наблюдения. Для фактических отправок все условия перепроверяет sender.

## Проверки

- Focused: 34 теста новых случаев добора и source cadence прошли.
- PostgreSQL: 4 теста прошли на отдельной tmpfs БД; конкурентный резерв и
  существующая сериализация работодателей проверены настоящими транзакциями.
- Миграция: upgrade с пустой БД, downgrade до `c72f9a31d5be`, повторный upgrade
  до `d830a4b26f19`; существующая оценка сохранена, оба новых JSON-поля
  `NOT NULL DEFAULT '[]'` получили пустые списки.
- Ruff check/format и mypy (`167` source files) прошли.
- Playwright: три последовательных чистых browser context; actual local API,
  сохранение формы, matching, preparation, fake provider, две компании,
  выполненная цель и обычный отбор следующего кандидата. Дополнительно реальный
  manual API отвергает запрос без Bearer и сохраняет закрытие с cooldown с Bearer.
- Полный `pytest`: **1448 passed, 21 skipped**, 590.62 секунды, exit code 0.
  Пропущены opt-in внешние сервисы/live calls; PostgreSQL concurrency проверен
  отдельно указанным выше прогоном из четырёх тестов.

Полный тестовый прогон однажды прерван `earlyoom` при одновременном Chromium;
это не успешная проверка. После завершения браузерных и PostgreSQL-проверок
тестовые контейнеры остановлены, полный pytest запущен отдельно.
Real email delivery и live crawling не включались.

## Эксплуатация и выкатка

Рабочие правила, диагностические команды и rollout/rollback описаны в
[руководстве добора](daily-minimum-catchup.md). Историческое исследование
сохранено без изменения исходного тела. Артефакты браузера находятся в
`/tmp/jobhunter-minimum-e2e-artifacts`; итоговые logs —
`/tmp/jobhunter-minimum-final-{pytest,browser,postgres,migration}.log`.

Тестовые контейнеры `jobhunter-minimum-test-postgres` и
`jobhunter-minimum-test-redis` удалены через их `--rm` после остановки.
Образы не строились. Production containers, PhoneGate и named volumes не менялись.
Перед новым build: `/` использует 68%, Docker build cache пуст.

Следующий шаг после всех проверок и коммита — отдельная авторизация выкатки.
Предыдущие разрешения относились к другим изменениям и не разрешают этот rollout.
Read-only сверка до выкатки: PROD checkout `cc7a77a` чистый; API/worker/beat
используют один текущий image ID
`sha256:4b6438c003659d33db6fe3489e3b2e17255c0c36a87061668159fd851665739f`.
Предлагаемое окно отката на этот образ — 24 часа; более старые неиспользуемые
JobHunter images после успешной новой выкатки не сохранять.
