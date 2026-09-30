# Проверка исправлений панели и Gmail consent

Изменения разработаны и проверены в локальном WSL checkout. PROD и PhoneGate
не изменялись. Миграции и настройки реальной доставки не менялись.

## Исправленное поведение

| Запрос | Реализация и проверка |
| --- | --- |
| Из уведомления выключенной автоотправки нельзя включить её | Ссылка ведёт в `settings#auto-send`. Общая кнопка включает, ставит на паузу или возобновляет отправку; после действия сохраняется профиль и раздел настроек. Проверены admin и user transports. |
| Белый прямоугольник за выбором профиля | Нативный select заменён themed popup с обычными ссылками. Проверены реальная доступность списка для клика, Escape/Enter, desktop/mobile, отсутствие горизонтального overflow и скриншоты. |
| На странице пользователей другой низ sidebar | Используется общий sidebar со статусами выбранного профиля. Счётчики пользователей/приглашений перенесены в содержимое страницы. |
| На странице пользователей расходится число отправок | Используется `daily_target_state`, как в основной панели: текущий локальный день, статус отправки и постоянные отказы доставки. Выбор профиля сохраняется при переходах; число не обрезается искусственно до лимита. |
| Кнопка Gmail запускает identity login | Новый `/admin/oauth/gmail/connect` требует admin-сессию, запрашивает Gmail send/read-only и identity scopes с явным consent. Legacy `consent=1` ведёт туда же; обычный Google-вход сохранён. |

## Результаты

- `ruff check .`: успешно.
- `ruff format --check .`: успешно.
- `mypy app fixture_site`: успешно, 167 файлов.
- Связанные UI/OAuth/notification тесты: 28 passed.
- Новые focused проверки: 2 passed.
- Полный `pytest` с `RUN_PLAYWRIGHT_TESTS=1`: **1452 passed, 19 skipped**, 625.94 с.
- Пропущены opt-in PostgreSQL/Redis, live Rabota/PhoneGate и реальные GSM проверки.

`tests/integration/test_panel_controls.py` проходит три последовательных полных
сценария в чистых Playwright Chromium contexts. Браузер открывает панель через
локальный HTTP server, входит паролем, выполняет действия с confirmation/CSRF,
меняет профиль, открывает пользователей и проходит Gmail consent. Отдельные чистые
user contexts трижды проверяют включение, паузу и возобновление своей автоотправки.

Локальный OAuth provider имеет HTTP authorize/grant/token endpoints. Token exchange
проверяет PKCE; ID token подписан временным RSA-ключом, а штатный Google verifier
проверяет подпись через локально заданный сертификат. Проверяются nonce, identity,
scopes, сохранённая credential и удаление binding cookie. Это браузерный E2E с
тестовым провайдером, без подключения реального Gmail и без отправки писем.

JUnit полного прогона: `/tmp/jobhunter-panel-final.xml`. Скриншоты локальной панели:
`/tmp/jobhunter-panel-preview/profile-dark-0.png` и `profile-mobile-0.png`.

## Предлагаемая выкладка и откат

Выкладка требует отдельного явного разрешения оператора в текущем разговоре по
[AGENTS.md](../AGENTS.md). Проверенный коммит сначала переносится в каноническую
DEV `main`; PROD `main` затем синхронизируется только fast-forward к точному DEV
`main` SHA. Текущий feature checkout не используется как reference синхронизации.

Перед сборкой фиксируются текущие image revision/digest и состояние сервисов,
проверяется диск и отсутствие активного звонка. По [runbook](deployment.md)
собирается один application image и переключаются все семь app services.
Новых миграций в этом изменении нет. После выкладки проверяются health/readiness,
очереди, одинаковые digests, обе панели, `docker system df` и `df -h /`; очищаются
только устаревшие unused JobHunter images/cache. Один предыдущий образ сохраняется
на согласованное окно отката.

Откат использует временный Compose override со старым image и соответствующим
`EXPECTED_APP_REVISION` для всех семи сервисов согласно deployment runbook.
Схема БД этим изменением не меняется. PROD Git остаётся чистым и fast-forward;
reset, cherry-pick и новые PROD-коммиты не применяются. После отката также проверяются
health и единый digest.
