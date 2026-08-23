# job-agent

`job-agent` — автономный модульный монолит для безопасного поиска вакансий,
сопоставления с подтверждённым профилем и подготовки/отправки откликов. Celery
worker и Beat работают на сервере независимо от ChatGPT, Gemini, Codex или
телефона пользователя; REST, мобильная server-rendered панель и удалённый MCP
служат интерфейсами управления.

> Реальный crawl Rabota.md и реальная Gmail-отправка по умолчанию выключены.
> Репозиторий не запускает массовый live scan и не отправляет реальные письма во
> время тестов. Перед live crawl оператор обязан проверить условия сайта и
> юридическое основание, а затем явно зафиксировать review.

## Архитектура

Основной поток не зависит от конкретного job board:

```text
source adapter → discovery/crawl → normalization → SourceJob
  → reversible deduplication → CanonicalJob
  → deterministic filters → strict LLM evaluation
  → verified contact → verified resume → application generator
  → deterministic policy → Gmail sender(application_id)
```

- `app/crawlers/` — registry, Rabota.md, Generic HTML, fixture, API/RSS/sitemap
  adapters, checkpoints, recheck и degradation circuit breaker;
- `app/models/`, `app/database/`, `migrations/` — SQLAlchemy 2, PostgreSQL и
  Alembic;
- `app/deduplication/` — исходные публикации остаются сохранёнными, canonical
  merge можно разъединить;
- `app/matching/`, `app/contacts/`, `app/applications/`, `app/policies/` — границы
  недоверенных данных и детерминированное решение;
- `app/email/` — OAuth 2.0/PKCE, зашифрованный refresh token, Gmail и test-only
  fake provider;
- `app/scheduler/` — Celery Beat, очереди, Redis locks и идемпотентность;
- `app/api/`, `app/admin/`, `app/mcp/` — REST, мобильная панель и MCP Streamable
  HTTP;
- `app/audit/`, `app/observability/` — audit events, JSON logs, health/readiness и
  Prometheus metrics.

Схема создаёт `UserProfile`, `Resume`, `JobPreference`, `JobSource`,
`SourceCategory`, `SourceJob`, `CanonicalJob`, `JobSnapshot`, `ScanRun`,
`BatchScanRun`, `MatchEvaluation`, `EmployerContact`, `Application`,
`EmailDelivery`, `OAuthCredential`, `AuditEvent`, `Alert` и `DailyReport`.
Начальная миграция находится в `migrations/versions/`.

## Быстрый локальный запуск

Требуются Docker Compose v2 и, для host-проверок, Python 3.12+ с `uv`.

```bash
cp .env.example .env
chmod 600 .env
uv sync --extra dev
uv run job-agent hash-password
uv run job-agent hash-api-key
```

Обе команды без аргумента читают секрет без echo. Сохраните Argon2 hash как
`ADMIN_PASSWORD_HASH` в одинарных кавычках, а SHA-256 bearer key — как JSON-массив
`MCP_API_KEYS_HASHED=["..."]`. Исходный bearer token храните отдельно и задайте
его клиенту как `JOB_AGENT_MCP_TOKEN`; сервер его не хранит. Также замените
`SECRET_KEY`. Не передавайте секреты в shell argv на общем хосте.

```bash
docker compose up --build -d postgres redis
docker compose run --rm migrate
docker compose run --rm --no-deps api job-agent seed
docker compose up --build -d api worker beat caddy
docker compose ps
```

После запуска доступны:

- панель: `http://localhost/`;
- OpenAPI: `http://localhost/api/docs`;
- health/readiness: `http://localhost/health` и `/ready`;
- MCP Streamable HTTP: `http://localhost/mcp`.

В production используются только HTTPS и уникальные secrets. Чистая локальная
БД также может быть создана напрямую:

```bash
uv run alembic upgrade head
uv run job-agent seed
uv run uvicorn app.main:app --reload
```

Worker и Beat запускаются отдельными процессами; точные команды есть в
`docker-compose.yml`.

## `.env`

Обязательные production-настройки:

- `ENVIRONMENT=production`;
- согласованные `POSTGRES_*` и `DATABASE_URL=postgresql+asyncpg://...` без
  example-пароля;
- `REDIS_PASSWORD` и пароль в `REDIS_URL`;
- `PUBLIC_BASE_URL=https://jobs.example.com` и
  `CADDY_ADDRESS=jobs.example.com`;
- случайный `SECRET_KEY`, `ADMIN_PASSWORD_HASH`, хотя бы один hash в
  `MCP_API_KEYS_HASHED`;
- идентифицирующий `CRAWLER_USER_AGENT` с адресом оператора.

Условные переменные:

- `LLM_PROVIDER=mock|openai|gemini`, соответствующий API key и явный model;
- `TOKEN_ENCRYPTION_KEY`, `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` — для Gmail и
  Google OIDC-входа;
- `GOOGLE_ADMIN_EMAILS=["operator@example.com"]` — точный allowlist Google-аккаунтов,
  которым разрешён вход в панель;
- `EMAIL_PROVIDER=gmail` и `REAL_EMAIL_DELIVERY_ENABLED=true` — только после
  отдельной приёмки;
- `EMERGENCY_EMAIL_KILL_SWITCH=true` немедленно блокирует provider path после
  recreation API/worker.

`.env.example` содержит development placeholders, а не production-секреты.
Production validation намеренно отклоняет известный placeholder secret,
не-HTTPS origin и example database password.

## Профиль, резюме и пожелания

Войдите в панель и используйте разделы `Профиль`, `Резюме` и `Предпочтения`.
Полный профиль, включая опыт, образование, права, языки и `confirmed_facts`, можно
заполнить через `PUT /api/v1/profile` или MCP `update_user_profile`; пример полей
есть в `config/preferences.example.yaml`.

PDF загружается через панель или `POST /api/v1/resumes`. Сервер проверяет размер,
расширение, MIME/magic bytes, безопасное имя, SHA-256 и путь. После ручной сверки
нажмите `Проверено` в панели: только active+verified resume может участвовать в
автоотправке. MCP принимает лишь metadata и никогда не принимает filesystem path.

Настройте отдельно:

- разрешённые и запрещённые категории;
- категории, разрешённые именно для auto-send;
- города, remote, графики, зарплату, языки и работу без опыта;
- `consider_outside_primary_resume=true` для явно разрешённой работы вне основной
  профессии;
- score и дневной лимит.

Безопасные значения по умолчанию: `auto_send_enabled=false`,
`global_pause=true`, пустой auto-send allowlist.

REST использует тот же bearer key:

```bash
export JOB_AGENT_BASE_URL=http://localhost
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${JOB_AGENT_MCP_TOKEN}" \
  "${JOB_AGENT_BASE_URL}/api/v1/status"
```

## Первый full scan Rabota.md

`job-agent seed` создаёт основной профиль с безопасными остановленными настройками,
а Rabota.md — как `enabled=false`, `PAUSED` и `automatic_actions_paused=true`.
Не считайте этот README юридическим разрешением.
Первый live scan выполняйте только после явного разрешения владельца установки и
актуального review, описанного в `docs/sources/rabota-md.md`:

1. вручную проверьте публичные условия, ограничения и дату review;
2. через аутентифицированный REST `PATCH /api/v1/sources/{id}` или MCP
   `update_source` передайте полный существующий config, добавив
   `live_mode=true`, `policy_review_acknowledged=true` и непустой
   `policy_review_reference` с датой/основанием/версией проверенных документов;
3. вызовите `validate_source(source_id)` — это минимальная публичная проверка, не
   full scan;
4. вызовите `enable_source(source_id)`; это включает только crawling и сохраняет
   `automatic_actions_paused=true`, а успешный scan ещё должен вернуть source в
   `HEALTHY`;
5. при необходимости проверьте `discover_categories(source_id)`;
6. только затем вызовите `start_full_scan(source_id)` и сразу получите `scan_id`;
7. следите через `get_scan_status(scan_id)`, панель, alerts и audit.

Планировщик продолжает incremental/full/recheck для включённого источника при
`automatic_actions_paused=true`, но после таких scan не запускает matching и
application pipeline. Downstream снимается с паузы отдельным явным изменением и
всё равно подчиняется `global_pause`, auto-send allowlist и delivery kill switch.

Поле `configuration` заменяется целиком: перед update сохраните schedules и
лимиты. `live_mode=false` является только тестовым seam с внедрённым fixture
transport и не может включить persisted Rabota.md source. CAPTCHA, login, 403,
429, изменение структуры или access policy приостанавливают источник; система не
закрывает вакансии массово.

## Подключение второго сайта

Скопируйте `config/sources/generic-example.yaml`, оставьте только реальный
разрешённый домен и заполните селекторы после исследования публичной структуры.
Шаблон намеренно содержит `null`, а не выдуманные селекторы.

```bash
uv run job-agent validate-source-config config/sources/my-source.yaml
```

Добавьте конфигурацию через `POST /api/v1/sources` или MCP `add_source`, затем
выполните `validate_source`, discovery и fixture/incremental scan. Для API, RSS,
sitemap и employer careers используйте зарегистрированные типы `generic_api`,
`rss`, `sitemap`, `company_careers`; matching, policy, Gmail и Applications менять
не нужно. Полный checklist находится в `docs/adding-source.md`.

## Gmail OAuth

1. Создайте Google OAuth Web client и разрешите точный callback
   `https://DOMAIN/api/v1/oauth/gmail/callback`.
2. Настройте client ID/secret, отдельный случайный `TOKEN_ENCRYPTION_KEY` и точный
   `GOOGLE_ADMIN_EMAILS` allowlist.
3. Оставьте `REAL_EMAIL_DELIVERY_ENABLED=false` и глобальную паузу включённой.
4. Откройте панель и нажмите `Войти через Google`. Один server-side callback
   проверит ID token, email allowlist, state и PKCE, сохранит refresh token только
   зашифрованно и выдаст отдельную admin-session cookie.
5. Проверьте Google identity и Gmail token в разделах `Обзор` и `Система`.

Запрашиваются OIDC `openid email` и единственный Gmail scope `gmail.send`. Sender принимает только
`application_id`; recipient, MIME, текст и verified resume выбирает сервер.
Подробности и процедура revoke: `docs/gmail-oauth.md`.

## MCP

Endpoint: `https://DOMAIN/mcp`, transport: Streamable HTTP, header:

```text
Authorization: Bearer <исходный JOB_AGENT_MCP_TOKEN>
```

Все tools требуют bearer auth; встроенные ключи пока не имеют отдельных ролей.
`start_full_scan` возвращает ID и работает асинхронно, а `send_application`
принимает только `application_id` и повторно проверяет policy/idempotency.
Настройки MCP Inspector, Codex и протокольные примеры для ChatGPT/Gemini-
совместимых клиентов приведены в `docs/mcp.md`; доступность UI конкретного клиента
не утверждается.

## Включение auto-send и аварийная пауза

Сначала прогоните E2E с mock LLM/fake Gmail, проверьте профиль, confirmed facts,
verified resumes, contacts, несколько писем, малый дневной лимит и узкий список
категорий. Затем нужны оба независимых разрешения:

1. deployment: `EMAIL_PROVIDER=gmail` и
   `REAL_EMAIL_DELIVERY_ENABLED=true`, после чего recreate API/worker;
2. user policy: категории auto-send, порог, лимит, `global_pause=false` и отдельное
   явное действие `resume_auto_send`/переключатель панели.

После этого только `auto_approved` не требует подтверждения каждого письма. LLM
не может дать это решение самостоятельно.

При инциденте сначала вызовите MCP `pause_auto_send` или переключатель панели.
Затем задайте `EMERGENCY_EMAIL_KILL_SWITCH=true` и
`REAL_EMAIL_DELIVERY_ENABLED=false`, пересоздайте как минимум API и worker и
проверьте `sending`/`delivery_unknown`. Пауза проверяется непосредственно перед
provider call.

## Проверки

```bash
uv sync --extra dev
./scripts/verify.sh
./scripts/demo-e2e.sh
uv run alembic upgrade head
uv run alembic check
docker compose config --quiet
docker compose build
```

Обычные тесты не зависят от Rabota.md и не отправляют письма. PostgreSQL/Redis/
Celery service-backed проверки запускаются отдельно (CI делает это
автоматически):

```bash
RUN_SERVICE_INTEGRATION_TESTS=1 \
DATABASE_URL=postgresql+asyncpg://job_agent:test-password@127.0.0.1:5432/job_agent_test \
REDIS_URL=redis://127.0.0.1:6379/15 \
uv run pytest tests/integration/test_infrastructure.py
```

Opt-in live smoke выключен по умолчанию, делает малый bounded crawl и не
отправляет отклики:

```bash
ENABLE_LIVE_RABOTA_SMOKE_TEST=true uv run pytest -m live
```

Запускайте его только после собственного актуального policy review.

## Production-развёртывание

После заполнения production `.env` и secret manager:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.prod.yml build --pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d postgres redis
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm migrate
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  run --rm --no-deps api job-agent seed
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  up -d api worker beat caddy
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

Перед upgrade поставьте auto-send на паузу, дождитесь/остановите writers, создайте
backup и используйте локальный `build --pull` либо отдельно настройте immutable
registry image — не смешивайте эти два workflow. Канонические команды backup и
restore находятся в `docs/operations.md`.

## Ограничения

- Сервис получает только публично доступные активные страницы; удалённые страницы,
  закрытые архивы и скрытые контакты недоступны.
- Исследование на 2026-08-03 не обнаружило разрешённого публичного API/RSS/sitemap
  Rabota.md и не установило однозначного разрешения на массовый crawl; live source
  поэтому fail-closed до review. Публичная глубина сайта может ограничить полноту.
- HTML меняется; degradation detector ставит источник на паузу, но адаптер иногда
  требует обновления fixtures/selectors вручную.
- Встроенный bearer auth однопользовательский и без scoped roles; для нескольких
  операторов нужен внешний OIDC/OAuth gateway.
- Alertmanager/канал уведомлений и автоматический off-host backup scheduler не
  входят в Compose; metrics доступны внутреннему scraper, Caddy закрывает
  публичный `/metrics`.
- Текущий token encryption использует один active key; штатного key-ring/
  re-encryption command нет. Для ротации поставьте отправку на паузу, отзовите OAuth
  и переподключите аккаунт с новым ключом.
- Playwright — только опциональный безопасный fallback и требует установки extra и
  browser binaries; основной crawler использует HTTP/HTML.
- Реальный LLM/Gmail и live Rabota.md не участвуют в CI. Mock/fake внешних систем
  не подменяет основную бизнес-логику.

## Документация

- [Архитектура](docs/architecture.md)
- [Безопасность](docs/security.md) и [модель угроз](docs/threat-model.md)
- [Краулеры](docs/crawlers.md) и [добавление источника](docs/adding-source.md)
- [Исследование Rabota.md](docs/sources/rabota-md.md)
- [Политика auto-send](docs/auto-send-policy.md)
- [Gmail OAuth](docs/gmail-oauth.md)
- [MCP](docs/mcp.md)
- [Обучение на решениях review](docs/review-learning.md)
- [Развёртывание](docs/deployment.md) и [операции](docs/operations.md)

## Правовые и технические границы

Система не проходит CAPTCHA или авторизацию, не снимает собственные rate limits и
не использует приватные API. Техническая доступность
URL сама по себе не является разрешением на массовое использование. Оператор
отвечает за условия каждого источника, персональные данные, частоту запросов и
правила коммуникации с работодателями.
