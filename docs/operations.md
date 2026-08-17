# Эксплуатация

Документ предназначен для оператора `job-agent`. Он дополняет, но не заменяет
runbook облачной платформы. Команды ниже предполагают стандартные имена сервисов
`api`, `worker`, `beat`, `postgres`, `redis`, `caddy`; сверяйте их с Compose-файлом.

## Безопасные операционные принципы

1. При неопределённости сначала включить глобальную паузу отправки.
2. Не повторять автоматически Application со статусом `delivery_unknown`.
3. Не закрывать вакансии массово при подозрении на поломку адаптера.
4. Не исправлять состояние прямым SQL без отдельного плана и backup.
5. Не публиковать в тикетах токены, тело резюме, полный текст письма или PII.
6. Ошибка одного источника не является основанием остановить здоровые источники.
7. Любое ручное изменение политики или повтор отправки должно попадать в аудит.

## Ежедневная проверка

Проверьте:

- `/health` и `/ready`;
- состояние контейнеров, worker и единственного Beat;
- свежесть последнего incremental scan каждого включённого источника;
- source health, parsing/network error rate, HTTP 403/429 и CAPTCHA/login redirects;
- длину очередей и возраст самого старого задания;
- Applications в `sending`, `delivery_unknown`, `failed`, `pending_review`;
- дневной счётчик отправок и срабатывание лимита;
- свободное место БД/хранилища резюме/backup;
- успешность последнего backup и последней тестовой реставрации;
- alerts и необработанные события безопасности.

Базовые команды:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --since=24h --tail=500 api worker beat
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T worker celery -A app.scheduler.celery_app:celery_app inspect ping
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T worker celery -A app.scheduler.celery_app:celery_app inspect active
```

Если путь Celery application в проекте иной, используйте фактический аргумент
запуска worker. Не выводите полное окружение контейнера в лог диагностики.

## Состояния сканирования

`start_full_scan` и `start_incremental_scan` должны быстро вернуть `scan_id`, а
длительная работа выполняется worker-ом. Состояние проверяется через панель, REST
или MCP `get_scan_status`. Для запуска всех источников используйте отдельную batch
операцию; отказ одного child scan не должен скрывать результаты остальных.

Оператору важны:

- тип и источник;
- checkpoint и момент его последнего обновления;
- количество entrypoints/pages/jobs;
- new/updated/unchanged;
- parsing/network errors;
- started/finished timestamp;
- причина pause/degraded/failed;
- correlation/scan id для поиска логов.

Не удаляйте checkpoint после временной ошибки. Полный scan должен продолжиться с
него, если конфигурация источника совместима. Если конфигурация или семантика
checkpoint изменилась, создайте новый run осознанно и сохраните прежний для
диагностики.

## Деградация источника

Признаки:

- внезапный нулевой результат;
- резкое падение количества вакансий;
- высокий parsing error rate;
- исчезновение обязательного элемента/ID;
- изменение структуры canonical URL;
- CAPTCHA, login redirect, 403 или длительный 429;
- изменение условий/access policy.

Порядок действий:

1. Отключите источник или его автоматические действия через панель/MCP.
2. Оставьте глобальную отправку включённой только если другие источники доказанно
   изолированы; при сомнении поставьте глобальную паузу.
3. Убедитесь, что recheck не переводит вакансии этого источника в `closed`.
4. Сохраните безопасную диагностику: HTTP status, URL без секретных query,
   selector/field name, агрегаты и correlation id. Не сохраняйте лишний HTML с PII.
5. Проверьте публичные условия вручную.
6. Обновите fixtures и адаптер, прогоните unit/integration тесты.
7. Выполните малый validate/smoke run с rate limit.
8. Возобновите incremental scan; full scan запускайте лишь после проверки и, для
   Rabota.md, без массового live-обхода без разрешения.

Одна сетевая ошибка не является подтверждением закрытия вакансии. Штатный переход
— `active → possibly_closed → closed` после настроенного числа подтверждённых
отсутствий или явного публичного сообщения о закрытии. Массовая аномалия адаптера
останавливает переходы.

## Очереди и зависшие задания

Перед перезапуском worker выясните, является ли задача идемпотентной и где хранится
её логический lock. Для scan безопасное повторное выполнение опирается на
checkpoint/upsert; для Application — на уникальный idempotency key и блокировку в
БД/Redis.

Если Application долго находится в `sending`:

1. не отправляйте её вручную;
2. проверьте EmailDelivery и Gmail provider message id;
3. найдите correlation/application id в логах;
4. если неизвестно, принял ли Gmail запрос, установите/сохраните
   `delivery_unknown`;
5. решение о повторе принимает оператор после проверки Gmail, не retry job.

Celery revoke не гарантирует остановку уже выполняющейся задачи. Не используйте
очистку всей очереди как стандартное средство устранения одной ошибки.

## Gmail и отправка

Аварийная последовательность:

1. `pause_auto_send` через панель или авторизованный MCP;
2. выключить server-side emergency/real-send switch в secret/config platform;
3. перезапустить только нужные процессы, если настройка читается при старте;
4. проверить, что новые решения становятся `pending_review`/`blocked`, а не sent;
5. проверить активные email-задачи и Gmail Sent вручную;
6. зафиксировать AuditEvent и incident timeline.

Пауза не должна отменять уже отправленное Gmail сообщение. Она обязана блокировать
новые отправки при финальной policy-проверке, в том числе задачи, поставленные в
очередь до паузы.

Классификация ошибок:

- 429/5xx/сетевой сбой до подтверждения — ограниченный retry с backoff и jitter;
- однозначный 4xx из-за recipient/payload/auth — `failed` либо требуется
  переподключение OAuth;
- timeout/обрыв после возможного принятия сообщения — `delivery_unknown`, без
  автоматического retry;
- revoked refresh token — остановить реальную отправку и переподключить аккаунт.

## Отчёты

После каждого цикла сверяйте агрегаты:

- проверено источников и страниц;
- найдено вакансий, новых и обновлённых;
- перепроверено старых;
- объединено дублей;
- подходящих;
- автоматически отправлено;
- ожидают проверки;
- пропущено/заблокировано;
- ошибки по типам.

Для каждого sent должны быть связаны Application, EmailDelivery, canonical/source
job, resume, verified contact, policy decision, score, timestamp и Gmail message
id. Отсутствие связи — инцидент целостности, а не повод создать вторую отправку.

## Логи, метрики и alerts

Логи должны быть JSON и включать timestamp, level, event, correlation id, а для
соответствующих событий — scan id/source id/application id. Запрещено логировать:

- OAuth access/refresh token и client secret;
- cookie/session/token авторизации;
- полный текст резюме или письма;
- сырой MIME;
- непроверенный HTML целиком;
- лишние телефоны, email и другие персональные данные.

Минимальные alerts:

- API not ready;
- worker/beat heartbeat отсутствует;
- очередь растёт или задача слишком стара;
- scan пропустил ожидаемое расписание;
- source health degraded/paused;
- parsing error rate превысил установленный порог;
- аномально нулевой/низкий результат;
- всплеск 403/429/5xx;
- email `delivery_unknown` или рост failed;
- дневной лимит неожиданно достигнут;
- backup не создан/слишком стар;
- мало диска, рост БД, истечение TLS.

Пороговые значения определяются baseline конкретного источника; не задавайте один
абсолютный порог для сайтов разного размера.

Текущая поставка только экспонирует process-local `/metrics` API внутри
backend-сети; Caddy намеренно отвечает `404` на публичный `/metrics`. Endpoint
показывает registry именно опрошенного API-процесса. Метрики, увеличенные Celery
worker-ом, не агрегируются в него автоматически, потому что multiprocess collector
или отдельный worker exporter не настроен. В Compose нет Prometheus, Alertmanager,
экспортёра очереди, планировщика уведомлений или off-host log sink. Счётчики
сбрасываются при рестарте; оператор должен добавить внешний сбор/агрегацию и
отдельную наблюдаемость worker/Beat/очереди. Перечень выше — требования к внешним
alert rules, а не уже настроенные уведомления.

HTTP rate limiter приложения также является process-local baseline с общей
корзиной, а не распределённым лимитом по ключу/роли или отдельными лимитами для
read/write/scan. При нескольких API-процессах и для публичного endpoint настройте
ограничения в Caddy/API gateway/WAF; внутренний limiter не является защитой от
распределённой нагрузки.

## Backup PostgreSQL

Канонический backup выполняет Compose-сервис профиля `ops`; пароль берётся из
окружения Compose и не передаётся аргументом процесса:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile ops run --rm backup
```

Сервис создаёт custom-format dump атомарно через временный файл, проверяет его
`pg_restore --list`, сохраняет в volume `backup_data` с mode `0600` и создаёт
соседний `.sha256`. Имя итогового файла выводится после успеха.

`backup_data` — named volume на том же Docker host, поэтому сам по себе не
защищает от потери/компрометации host. В поставке нет расписания, retention,
шифрования или автоматического off-host upload. Настройте внешний scheduler и
экспортируйте `.dump` вместе с `.sha256` в отдельное зашифрованное object/off-host
хранилище, затем регулярно проверяйте доступность копии. Отдельно резервируйте
хранилище резюме с сохранением SHA-256/метаданных и ключ шифрования OAuth в secret
manager. Не храните ключ рядом с зашифрованными токенами в том же архиве.

## Тестовое восстановление

Восстановление использует только `.dump` непосредственно из того же
`backup_data`. Оно очищает существующие объекты целевой БД, поэтому сначала
создайте отдельную тестовую БД и укажите точное подтверждение:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres \
  sh -ec 'createdb --username="$POSTGRES_USER" job_agent_restore_test'

docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile ops run --rm \
  -e POSTGRES_DB=job_agent_restore_test \
  -e BACKUP_FILE=/backups/job-agent-job_agent-YYYYMMDDTHHMMSSZ.dump \
  -e RESTORE_CONFIRM=restore:job_agent_restore_test \
  restore
```

Замените имя файла на фактическое значение команды backup. Restore проверяет
`.sha256`, если он существует, валидирует архив и использует `--clean`,
`--if-exists`, `--exit-on-error`, `--no-owner` и `--no-acl`. Значение
`RESTORE_CONFIRM` должно в точности совпадать с `restore:<POSTGRES_DB>`; служебные
БД PostgreSQL запрещены.

Затем:

1. проверьте миграционную ревизию;
2. выполните read-only проверки количества ключевых сущностей и связей;
3. проверьте чтение нескольких файлов резюме по сохранённому SHA-256;
4. не запускайте worker/beat и реальную отправку против restored БД;
5. удалите тестовые данные согласно политике retention.

Для восстановления рабочей БД сначала включите глобальную паузу и server-side
real-send kill switch, остановите `api`, `worker` и `beat`, сохраните текущую БД
отдельным backup и только затем запускайте тот же `restore` с явно указанной
целевой БД. Не выполняйте destructive restore поверх работающего приложения.

## Ротация ключей и credentials

- Session secret: реализация использует один активный ключ без key ring; его
  замена инвалидирует существующие admin-сессии.
- MCP/API bearer: конфигурация принимает список SHA-256 hash. Для перехода можно
  временно добавить hash нового значения, перезапустить API, заменить клиентский
  secret, затем удалить старый hash и снова перезапустить. В БД нет владельцев,
  ролей или отдельного revoke-записа для этих ключей.
- OAuth client secret: обновите secret store и переподключите при необходимости.
- Token encryption key: текущая реализация использует один
  `TOKEN_ENCRYPTION_KEY`, без key ID, key ring и команды перешифрования. Для
  плановой бесшовной ротации сначала нужно реализовать версионированное хранилище
  и миграцию; документация не должна изображать такую возможность существующей.
- PostgreSQL/Redis credentials: обновите сервер и consumers согласованно.

При компрометации token encryption key поставьте глобальную паузу, отключите
real-send, отзовите Google token, сохраните необходимый audit/backup, замените ключ
и заново пройдите Gmail OAuth. Простая замена делает существующий ciphertext
нечитаемым; штатной online re-encryption процедуры сейчас нет.

## Инциденты

### Подозрение на утечку OAuth

1. глобальная пауза и server-side real-send off;
2. revoke token в аккаунте Google;
3. ротация client secret/ключей при необходимости;
4. поиск обращений без раскрытия самих токенов;
5. оценка отправленных сообщений через Gmail и EmailDelivery;
6. документирование и безопасное уведомление владельца.

### Подозрение на повторную отправку

1. немедленная пауза;
2. не удалять Application/EmailDelivery/audit;
3. сравнить idempotency keys и Gmail message ids;
4. проверить locks, retry и ручные действия;
5. исправить причину и добавить regression test до возобновления.

### Подозрение на prompt injection

1. заблокировать вакансию/Application;
2. сохранить только необходимое безопасное evidence;
3. убедиться, что recipient/resume/policy не были взяты из текста вакансии;
4. проверить audit и отсутствие MCP/email side effects;
5. добавить обезличенный fixture и regression test.

### Компрометация администратора или MCP token

1. отозвать credential и сессии;
2. глобальная пауза;
3. проверить AuditEvent: profile, preferences, source, approvals, sends;
4. восстановить политики из проверенного состояния, не удаляя аудит;
5. усилить MFA/сетевые ограничения внешнего identity provider, если используется.

## Плановое обслуживание

- ежедневно: health/alerts/scan/email/backup;
- еженедельно: full scan по разрешённому расписанию, review degraded sources,
  pending reviews и storage growth;
- ежемесячно: restore drill, dependency/security updates, access review, audit
  sampling, source policy review;
- ежеквартально: key rotation exercise, disaster recovery drill, threat-model
  review, проверка retention и прав операторов.

## Передача дежурства

Передайте следующему оператору: текущую глобальную паузу и real-send switch,
активные/зависшие runs, `delivery_unknown`, degraded sources, незавершённые
инциденты, последнюю успешную резервную копию/restore drill и запланированное
обслуживание. Не передавайте секреты через текст отчёта.
