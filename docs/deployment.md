# Развёртывание

Актуализировано 2026-09-30. Процедуры выполняются после локальной проверки и
разрешения оператора согласно `AGENTS.md`. DEV/PROD границы и текущий снимок —
[environments.md](environments.md). Источником
истины для имён контейнеров и переменных остаются `docker-compose.prod.yml` и
`.env.example`: перед первым развёртыванием сверяйте команды ниже с ними. Ни один
пример не содержит рабочего секрета.

## Модель развёртывания

Проект разворачивается как модульный монолит с отдельными процессами:

- FastAPI обслуживает REST, административную панель, OAuth callback и MCP;
- `worker` — очереди `crawling,celery`;
- `matching-worker` — `matching,applications`;
- `proxy-worker` — `proxy-maintenance`;
- `control-worker` — `email,reports,maintenance,phone`;
- `call-agent` — отдельный процесс телефонного канала;
- Celery Beat публикует периодические задания;
- PostgreSQL хранит бизнес-данные, checkpoints и аудит;
- Redis используется как broker/result backend и для распределённых блокировок;
- Caddy завершает TLS и проксирует запросы к API;
- загруженные резюме и резервные копии находятся в постоянных хранилищах.

Ноутбук пользователя после развёртывания не участвует в работе системы. Для
непрерывной работы нужны постоянно доступный сервер, DNS, PostgreSQL, Redis и
процессы API/worker/beat.

## Предварительные условия

- Linux-сервер с актуальными Docker Engine и Compose v2;
- доменное имя, указывающее на публичный адрес сервера;
- открытые входящие TCP 80/443; PostgreSQL и Redis не публикуются в интернет;
- достаточно диска для БД, резюме, логов и резервных копий;
- отдельная непривилегированная учётная запись для эксплуатации;
- SMTP не требуется: реальная доставка идёт только через Gmail API;
- OAuth client и секреты нужны лишь при включении реальной Gmail-интеграции.

Не запускайте production на хосте, где недоверенные пользователи имеют доступ к
Docker socket: членство в группе `docker` практически эквивалентно root-доступу.

## Подготовка конфигурации

```bash
cp .env.example .env
chmod 600 .env
```

Заполните все обязательные значения из `.env.example`. Сгенерируйте разные
случайные значения как минимум для ключа сессий/API-аутентификации, шифрования
OAuth-токенов и пароля PostgreSQL. Не копируйте ключи из примеров документации.
Секреты предпочтительно передавать через secret manager платформы или Docker
secrets; `.env` допустим только на защищённом хосте и не должен попадать в Git,
образ, резервную копию исходников или диагностический архив.

Безопасные начальные значения:

- реальная отправка выключена;
- пользовательская политика auto-send выключена;
- глобальная пауза включена до завершения проверки;
- fixture/fake Gmail используется только в development/test;
- Rabota.md не сканируется массово во время проверки установки;
- MCP доступен только по HTTPS и требует аутентификацию.

Проверьте итоговую конфигурацию без вывода секретов в терминал или CI-лог. Не
используйте `docker compose config` в общедоступном логе: команда может раскрыть
подставленные значения.

## Первое production-развёртывание

Команды выполняются из `/srv/jobhunter-prod`; на установленном host нужен `sudo`.
Перед первой установкой оператор отдельно создаёт runtime/migrator PostgreSQL
roles, защищённый `/etc/jobhunter/migrator.env` и корректный `.env`. Compose
не является автоматическим provisioning этих credentials. После согласования:

```bash
sudo ./deploy/prod-compose.sh config --quiet
sudo ./deploy/prod-compose.sh build --pull api
sudo ./deploy/prod-compose.sh up -d postgres redis
sudo ./deploy/prod-compose.sh run --rm migrate
sudo ./deploy/prod-compose.sh up -d --no-build --wait --wait-timeout 180 \
  api worker matching-worker proxy-worker control-worker beat call-agent
sudo ./deploy/prod-compose.sh ps
```

Production-сервисы приложения используют отдельный immutable-style namespace
`jobhunter-prod:<git-sha>`. Wrapper `deploy/prod-compose.sh` вычисляет 12-символьный
SHA текущего PROD HEAD и экспортирует его как `JOBHUNTER_IMAGE_TAG`; прямой запуск
production Compose без этого тега намеренно отклоняется. DEV использует отдельный
namespace `jobhunter-dev:*`, поэтому DEV build больше не может перетереть PROD image.

### Синхронизация кода DEV → PROD

Каноническая DEV-ссылка для production-кода — строго
`/home/andrei/JobHunter:refs/heads/main`. Текущий checkout DEV может находиться на
feature-ветке и не участвует в health-check синхронизации.

Read-only проверка выполняется командой:

```bash
./deploy/check-code-sync.sh
```

Состояния `equal` и `dev_ahead` не являются ошибкой. `dev_behind` и `diverged`
требуют вмешательства. `unknown` означает, что отношение нельзя безопасно доказать
доступными локальными объектами Git; проверка ничего не fetch-ит и не должна
превращать это состояние в аварийный алерт.

Штатное обновление production-кода выполняется из `/srv/jobhunter-prod` только
fast-forward:

```bash
./deploy/sync-prod-code.sh
```

Скрипт требует чистый PROD working tree и ветку `main`, читает именно DEV
`refs/heads/main` и отказывается от merge/cherry-pick/reset при расхождении истории.
Обычный production checkout не должен создавать новые commits и не должен push-ить
изменения обратно в DEV.

Каждый production image содержит build flavor и revision. Container entrypoint
проверяет их до запуска процесса. API/workers дополнительно требуют DB-role
`jobhunter_app`, а `migrate` получает credentials из `/etc/jobhunter/migrator.env`
и требует `jobhunter_migrator`. При несовпадении роли, flavor или revision контейнер
завершается до Alembic/application startup.

### PhoneGate без нового host listener

Первичная активация — отдельная авторизованная PROD-процедура оператора.
JobHunter DEV не запускает её ради live-тестов и не меняет `/srv/phonegate/.env`,
credentials или `phonegate.service`. Токен для DEV передаёт оператор отдельно.

PhoneGate остаётся привязан к `127.0.0.1:8888`. JobHunter обращается к уже
существующему HTTPS endpoint Caddy через `docker-compose.phonegate.prod.yml`;
отдельный TCP relay и публикация порта PhoneGate не требуются.

Активация выполняется root-командой:

```bash
./deploy/activate-prod-phonegate.sh
```

Скрипт читает токен из локального `/srv/phonegate/.env`, атомарно создаёт
`/etc/jobhunter/secrets/phonegate-auth-token` и монтирует его только в `api`,
`control-worker` и `call-agent`. Токен не записывается в JobHunter `.env`, image
или environment контейнера. До перезапуска сервисов выполняются TLS, auth и
device-status проверки. TTS не вызывается во время preflight: такой запрос имеет
внешний эффект и проверяется только во время контролируемого реального звонка.

После успешной проверки создаётся root-owned marker
`/etc/jobhunter/phone-agent-enabled`. Обычный `deploy/prod-compose.sh` видит marker
и автоматически добавляет PhoneGate overlay во время последующих rollout.
Удаление marker и пересоздание `api`, `control-worker`, `call-agent` базовым
wrapper выключает интеграцию, не меняя схему БД и основной PROD `.env`.

Миграции должны завершиться успешно до запуска worker/beat. Команда использует
одноразовый сервис `migrate`; фактическую команду Alembic и revision проверяйте в
Compose-файле.

Не создавайте параллельно несколько экземпляров Beat: иначе одинаковые
периодические задания будут опубликованы несколько раз. Распределённые locks и
idempotency остаются обязательной второй линией защиты, но не заменяют это
операционное правило.

## DNS, TLS и reverse proxy

1. Создайте A/AAAA-запись домена.
2. Укажите этот домен и публичный origin в настройках приложения/Caddy.
3. Разрешите Caddy получить сертификат через ACME.
4. Проксируйте API, HTML-панель, OAuth callback и `/mcp` без удаления
   необходимых MCP-заголовков.
5. Ограничьте размер тела запроса на уровне proxy согласованно с лимитом загрузки
   резюме.

Не проксируйте наружу порты PostgreSQL, Redis или внутренний worker dashboard.
HTTP следует перенаправлять на HTTPS. За proxy должны корректно обрабатываться
только доверенные `Forwarded`/`X-Forwarded-*` заголовки; приложение не должно
доверять им от произвольного клиента.

В поставляемом Compose Uvicorn запущен с `--forwarded-allow-ips=*`. Это допустимо
только при контролируемом ingress. PROD публикует API исключительно на
`127.0.0.1:8091`; системный Caddy на текущем host проксирует его снаружи.
Compose Caddy включается отдельно профилем `container-edge`. Wildcard остаётся широкой
границей доверия: любой скомпрометированный или недоверенный контейнер в этой сети
может подделать forwarded-заголовки. Не подключайте к backend-сети посторонние
контейнеры; для общей или мультиарендной сети замените wildcard точным адресом/CIDR
proxy либо отключите обработку proxy headers. Caddy также должен заменять, а не
слепо сохранять, forwarded-заголовки внешнего клиента.

Для Streamable HTTP proxy не должен буферизовать долгие потоковые ответы и должен
передавать `MCP-Session-Id`, `Last-Event-ID`, `Authorization`, `Origin` и
стандартные content-type заголовки. Таймауты задавайте достаточными для MCP-сессии,
но сами длительные сканирования всегда выполняются асинхронно worker-ом.

## Проверка после запуска

Проверьте ожидаемые endpoints через публичный HTTPS-origin:

```bash
curl --fail --silent --show-error https://job-agent.example/health
curl --fail --silent --show-error https://job-agent.example/ready
```

Замените домен на свой. `/health` подтверждает жизнь web-процесса, `/ready` —
доступность необходимых зависимостей в пределах реализованной readiness-проверки.
Дополнительно проверьте:

```bash
./deploy/prod-compose.sh ps
./deploy/prod-compose.sh logs --tail=100 api worker matching-worker proxy-worker control-worker beat call-agent
./deploy/prod-compose.sh exec -T api alembic current
./deploy/prod-compose.sh exec -T worker celery -A app.scheduler.celery_app:celery_app inspect ping
```

Имя Celery application может отличаться; если так, используйте значение из
команды контейнера. Успешный HTTP health сам по себе не доказывает работу Beat,
worker, locks или Gmail.

На production после rollout проверяйте страницы и сохранённые данные read-only:
входные страницы, обе панели, разделы, отсутствие ошибок, intended image digest,
health/readiness и очередь. Смотрите Gmail status без mint нового token и не
создавайте/отмечайте реальные alerts ради smoke.

Приёмку с изменением тестовых данных выполняйте в DEV/staging до rollout:

1. создайте тестовый профиль, оставьте real send выключенным;
2. загрузите и подтвердите тестовое резюме, проверьте lifecycle gates;
3. выполните fixture scan и проверьте source health/audit;
4. выполните application/email E2E с fake Gmail;
5. проверьте общий Google-вход и отдельно Gmail connect по
   [gmail-oauth.md](gmail-oauth.md); учитывайте известное ограничение admin link;
6. для browser integration выполните три последовательных прохода в чистых contexts.

## Постоянные данные

В production постоянными должны быть:

- каталог данных PostgreSQL;
- хранилище загруженных резюме;
- данные Caddy/ACME;
- при необходимости экспортные резервные копии.

Redis не является источником истины для вакансий или отправок. Его потеря может
привести к потере очереди/locks, поэтому после восстановления требуется сверка
`ScanRun`, `Application` и `EmailDelivery`; идемпотентность в PostgreSQL должна
предотвращать повторную логическую отправку.

Никогда не включайте каталог резюме в публичную статику. Резюме выдаётся только
авторизованным кодом и выбирается сервером по `resume_id` из Application.

## Проверка и обновление

Разработку выполняйте в DEV. До любой PROD mutation завершите relevant focused
checks, `ruff check .`, `ruff format --check .`, `mypy app fixture_site`, `pytest`
и локальный E2E. Browser/auth/OAuth/cookie изменения требуют три подряд успешных
Playwright прохода в чистых contexts. Production smoke не заменяет этот gate.
Сообщите оператору результаты и план rollout/rollback; получите разрешение на
конкретное изменение. Предыдущее разрешение не распространяется на новый runtime.

Перед rollout проверьте чистый PROD `main`, DEV canonical `main`, active scans,
email tasks и активные звонки. Для schema/backfill изменений нужен проверенный
backup и согласованная пауза. Зафиксируйте старый image SHA/digest и совместимость
схемы. Не прерывайте активный звонок для обычного обновления.

```bash
cd /srv/jobhunter-prod
./deploy/check-code-sync.sh
./deploy/sync-prod-code.sh
sudo ./deploy/prod-compose.sh config --quiet
sudo ./deploy/prod-compose.sh build api
sudo ./deploy/prod-compose.sh run --rm migrate
sudo ./deploy/prod-compose.sh up -d --no-deps --no-build --wait --wait-timeout 180 \
  api worker matching-worker proxy-worker control-worker beat call-agent
```

Все семь app services используют один image, поэтому его достаточно собрать через
`build api`. При несовместимой миграции сначала остановите все затронутые процессы
(четыре worker, beat, call-agent и при необходимости API), а не только crawler.
`--no-deps` для переключения допустим после успешного migration и проверки
PostgreSQL/Redis. Ошибка migration запрещает запуск старого кода против новой схемы.

Сравните `.Image` каждого app container с intended digest, `APP_REVISION` и label,
проверьте HTTP health/readiness, четыре Celery pong и обе панели desktop/mobile.
Не считайте Docker process health отдельного worker доказательством работы очереди.
Source recovery и notification actions сначала проверяются в локальном E2E;
не отмечайте реальные PROD alerts прочитанными только ради smoke.

## Откат и Docker hygiene

Сохраните максимум один предыдущий JobHunter image на явно установленное окно
(обычно 24 часа). На текущем host автоматическая cleanup удаляет unused старые
images; для окна отката удерживайте предыдущий image созданным, но не запущенным
container без secrets/network/volumes. После окна удалите holder и image, если
он не используется. Не удаляйте image, на котором реально выполнен rollback.

Rollback должен сохранить чистую fast-forward PROD `main`. Используйте отдельный
временный Compose override для **всех семи** services: старый `image` плюс
соответствующий `EXPECTED_APP_REVISION`. Базовый wrapper ожидает SHA текущего Git
HEAD, поэтому подмена одного image/tag без override revision не является откатом.
Не делайте reset/cherry-pick/new commit в PROD. Применяйте override после штатных
Compose files и PhoneGate overlay, затем `up -d --no-deps --no-build --wait` для
семи services. Перед этим обязательно докажите совместимость старого кода с БД.
Для несовместимой схемы восстанавливайте согласованный backup отдельно по runbook.

После успешной сборки/rollout проверьте:

```bash
docker system df
df -h /
```

Удалите только obsolete unused JobHunter images/dangling layers и unused build
cache (`docker buildx prune --all --force`), сохранив один rollback image.
Не используйте `docker system prune -a`, не prune-ьте named volumes, PostgreSQL,
резюме и unrelated images. Заполнение `/` выше 70% требует расследования до
следующей сборки. Все app services должны остаться на одном intended digest.

Обновление только `docs` после checks переносится через DEV `main` → PROD
fast-forward, без image build, миграции и restart: `docs` не включены в runtime
Dockerfile. Зафиксируйте текущую running app revision отдельно от docs-коммита;
при следующем runtime rollout wrapper соберёт image для новой canonical `main`.

## Масштабирование

API и worker можно масштабировать независимо, если все процессы используют одну
БД/Redis и общедоступное защищённое хранилище резюме. При масштабировании:

- оставьте ровно один Beat;
- не запускайте два full scan одного source — это обеспечивается также lock-ом;
- задайте Celery concurrency ниже суммарной разрешённой конкуренции источников;
- не масштабируйте email queue без проверки блокировки одной Application;
- используйте разные очереди для crawling и email при высокой нагрузке;
- сохраняйте единый timezone для дневных лимитов и отчётов.

## Резервное копирование и восстановление

Канонический путь — Compose-сервисы профиля `ops`, а не прямой `pg_dump` с host:

```bash
./deploy/prod-compose.sh \
  --profile ops run --rm backup
```

Файлы создаются во внутреннем volume `backup_data`; это не off-host backup.
Команды восстановления, checksum-проверка и обязательный перенос копии во внешнее
зашифрованное хранилище описаны в `operations.md`. Backup считается пригодным
только после тестового восстановления в изолированную БД. Шифровальный ключ OAuth
необходимо резервировать отдельно в secret manager: дамп с зашифрованным refresh
token без соответствующего ключа бесполезен, а совместное хранение дампа и ключа
увеличивает риск компрометации.

## Облачные ограничения

- не используйте ephemeral filesystem для БД или резюме;
- запрещайте egress к private/link-local/metadata адресам как на уровне приложения,
  так и сетевой политикой хоста/облака;
- настройте автоматический restart, мониторинг диска и срок действия сертификата;
- подключите внешний scraper/alert manager: Compose публикует внутренние метрики,
  но не содержит Prometheus, Alertmanager или канал уведомлений;
- перенесите backup из host-local `backup_data` во внешнее зашифрованное
  хранилище и настройте retention/schedule средствами платформы;
- синхронизируйте время через NTP;
- ограничьте доступ операторов принципом наименьших привилегий;
- храните журнал аудита дольше обычных application-логов;
- учитывайте применимые условия сайтов, GDPR/местные требования и срок хранения
  персональных данных.

## Production checklist

- [ ] секреты уникальны и не находятся в Git/образе;
- [ ] real Gmail send и auto-send выключены при первом старте;
- [ ] БД и Redis не опубликованы;
- [ ] TLS работает, cookies имеют Secure/HttpOnly/SameSite;
- [ ] MCP и панель требуют аутентификацию;
- [ ] миграции применены к чистой или резервно скопированной БД;
- [ ] worker отвечает, запущен ровно один Beat;
- [ ] persistent volumes, внешний backup schedule и off-host copy настроены;
- [ ] внешний scraper/alert pipeline проверен тестовым безопасным событием;
- [ ] fixture/fake providers не включены в production-поток;
- [ ] source rate limits и публичные условия проверены;
- [ ] выполнено тестовое восстановление backup;
- [ ] задокументирован аварийный владелец глобальной паузы.
