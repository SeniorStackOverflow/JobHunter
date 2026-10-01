# Окружения DEV и PROD

Сверено 2026-10-01. Этот документ описывает проверенные границы окружений;
операционные команды обновления находятся в [deployment.md](deployment.md).

## Репозитории и версии

| Объект | Значение |
| --- | --- |
| Канонический DEV репозиторий | `/home/andrei/JobHunter` |
| Каноническая ветка | `refs/heads/main` в DEV репозитории |
| PROD checkout | `/srv/jobhunter-prod`, ветка `main` |
| Namespace DEV image | `jobhunter-dev:*` |
| Namespace PROD image | `jobhunter-prod:<12-символьный SHA>` |
| Runtime и семантика проверок | Python 3.12 |
| Публичный PROD origin | `https://jobhunter.46-225-103-75.sslip.io` |
| PROD API на host | `127.0.0.1:8091` |

DEV checkout может находиться на feature-ветке. Это не меняет источник
синхронизации: `deploy/check-code-sync.sh` сравнивает именно DEV `main`, а
`deploy/sync-prod-code.sh` переносит её в чистый PROD checkout только fast-forward.
Main может быть checkout-нута в отдельном worktree: проверяйте `git worktree list`
и сливайте завершённый коммит там, не сбрасывая другие ветки.

## Проверенный снимок PROD

На момент актуализации семь сервисов приложения (`api`, `worker`,
`matching-worker`, `proxy-worker`, `control-worker`, `beat`, `call-agent`) здоровы
и используют образ `jobhunter-prod:8167300ae75d` (rollout 2026-10-01). Alembic head —
`b7f1e4c9a2d3`.
Runtime PostgreSQL role — `jobhunter_app`; миграции используют
`jobhunter_migrator` через защищённый `/etc/jobhunter/migrator.env`.

Включены аккаунты, регистрация по приглашениям и телефонный агент. Основной образ
не устанавливает Chromium; Rabota browser fallback в production Compose — `none`.
На этом host TLS завершает системный Caddy, который проксирует loopback API.
Caddy-сервис из Compose не запущен. Эти наблюдения не заменяют проверку состояния
перед следующим rollout и не утверждают, что внешние OAuth/телефонные тесты выполнены.

Обновление только `docs` синхронизирует Git, но не требует нового image или
перезапуска приложения: Dockerfile не копирует `docs` в runtime. Поэтому docs-коммит
может быть новее записанной app revision. Перед следующим изменением runtime
соберите image для текущей PROD `main`; wrapper ожидает именно её SHA.

## Безопасная локальная проверка

Используйте отдельную тестовую БД и fake/mock providers; не подключайте тесты к
production DB, Gmail или PhoneGate. Полный проверочный набор:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy app fixture_site
env ENVIRONMENT=test DATABASE_URL=sqlite+aiosqlite:///:memory: \
  REDIS_URL=redis://127.0.0.1:6379/15 LLM_PROVIDER=mock EMAIL_PROVIDER=fake \
  REAL_EMAIL_DELIVERY_ENABLED=false EMERGENCY_EMAIL_KILL_SWITCH=false \
  PHONE_AGENT_ENABLED=false PHONE_AUTO_ANSWER_ENABLED=false \
  ENABLE_REALCALL_TESTS=false ENABLE_LIVE_PHONEGATE_SMOKE_TEST=false \
  ENABLE_LIVE_RABOTA_SMOKE_TEST=false RUN_SERVICE_INTEGRATION_TESTS=0 \
  RUN_PLAYWRIGHT_TESTS=1 .venv/bin/pytest
```

Для браузерных тестов установите dev + playwright extras и Chromium в DEV:
`uv sync --extra dev --extra playwright`, затем
`.venv/bin/playwright install chromium`. Live/service integration проверки
отдельно opt-in; их пропуск в обычном прогоне ожидаем.

DEV Compose не является запущенным DEV окружением сам по себе. Используйте
отдельный Compose project, свои порты, credentials, DB/Redis и volumes; проверяйте
фактические overrides. Не подставляйте PROD `.env` в локальные команды.

## Внешний PhoneGate

PhoneGate — внешняя production-зависимость. JobHunter DEV не меняет
`/srv/phonegate/.env`, не ротирует её секреты и не перезапускает `phonegate.service`.
Live DEV-проверка допустима только с отдельным разрешением и токеном, переданным
оператором. Наличие телефонного агента в PROD не даёт такого разрешения DEV.
