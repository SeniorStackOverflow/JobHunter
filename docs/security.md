# Безопасность

`job-agent` обрабатывает персональные данные, резюме, недоверенный HTML и право
отправлять письма от имени пользователя. Безопасность построена по принципам
минимальных привилегий, fail closed, разделения ответственности, defence in depth
и проверяемого аудита.

Модель угроз и остаточные риски описаны в `threat-model.md`; этот документ задаёт
обязательный эксплуатационный baseline.

## Доверительные границы

Недоверенными считаются:

- HTML, JSON/JSON-LD, RSS/API payload и headers источника;
- любой URL, redirect, canonical/employer/application link;
- описание вакансии, контакт и текст от работодателя;
- LLM response, включая синтаксически валидный JSON;
- MCP/REST/admin input до аутентификации и валидации;
- filename, MIME и содержимое загруженного файла;
- provider error/response;
- Redis queue message без проверки доменного состояния.

Доверие не переносится автоматически. Публичный email не становится verified,
валидный LLM result не становится policy approval, а Celery task не становится
правом отправки.

## Аутентификация и авторизация

Административная панель требует аутентификацию. Пароль хранится как стойкий
адаптивный hash с уникальной солью; plaintext/default development password
запрещён в production. При возможности внешний identity provider должен применять
MFA и короткий список операторов.

Сессия:

- случайный session identifier/подписанный токен достаточной энтропии;
- `Secure`, `HttpOnly`, подходящий `SameSite`;
- ротация после login/изменения привилегий;
- абсолютный и idle timeout;
- invalidation при logout/компрометации;
- отсутствие токена в URL/логах.

MCP и REST API используют один настроенный allowlist SHA-256 hash bearer keys.
Каждый валидный key имеет доступ ко всем опубликованным API/MCP операциям: в
текущей реализации нет owner, read/write roles, scopes или записи отзыва в БД.
HTML-панель использует отдельную подписанную admin session. Основной login получает
её после server-side проверки Google ID token, одноразового nonce и точного email
allowlist; password login сохранён как аварийный fallback. Google access/refresh
token никогда не становится значением admin session. Если deployment требует
разделения read/write/autosend, его необходимо реализовать в приложении либо
обеспечить проверенным identity gateway перед endpoint; имя или отдельное значение
встроенного bearer не делает его менее привилегированным.

Ответ 401/403 не раскрывает существование пользователя, hash или секретные
детали. Встроенный rate limiter — общий process-local лимит по адресу клиента для
всех HTTP routes. Он не распределён между процессами и не предоставляет отдельные
лимиты login/OAuth/MCP/write или per-key quota. Публичный production endpoint
требует соответствующих ограничений на Caddy/API gateway/WAF.

## CSRF и browser security

Все state-changing HTML формы используют непредсказуемый CSRF token, привязанный к
сессии и сроку. Обычные GET не меняют доменные настройки; OAuth start создаёт только
короткоживущую одноразовую authorization-запись с state/PKCE/browser binding.
Проверяются `Origin`/`Referer` там, где это безопасно, но они не заменяют token.

Security headers через приложение/proxy:

- Content-Security-Policy без unsafe inline, насколько позволяет HTMX/Jinja;
- `frame-ancestors 'none'` либо явный необходимый allowlist;
- `X-Content-Type-Options: nosniff`;
- строгая Referrer-Policy;
- HSTS только после подтверждения HTTPS для домена;
- ограниченная Permissions-Policy;
- запрет cache для OAuth/admin ответов с PII.

Пользовательский/внешний HTML не рендерится через `|safe`. Для vacancy description
предпочтителен escaped plain text; если нужен allowlist sanitizer, разрешается
минимальный набор тегов/атрибутов без script/style/event handlers/опасных URL.

## Секреты

Секретами являются как минимум:

- session/API signing keys;
- MCP bearer values;
- PostgreSQL/Redis credentials;
- Gmail OAuth client secret и refresh/access tokens;
- token encryption keys;
- LLM provider keys;
- backup encryption keys.

Требования:

- `.env` не коммитится и имеет `0600`;
- production использует secret manager/Docker secrets по возможности;
- секрет не передаётся в command arguments, URL, exception или metric label;
- разные environments/назначения имеют разные значения;
- ротация и отзыв документированы;
- CI использует fake providers и ephemeral credentials;
- secret scanning запускается до merge;
- diagnostic bundle проходит redaction.

Не выводите `docker compose config` в публичный CI-log. Доступ к Docker socket,
secret manager и backup ограничен.

## Шифрование OAuth

Refresh token шифруется и аутентифицируется Fernet с ключом, производным от одного
`TOKEN_ENCRYPTION_KEY`. Fernet создаёт случайность для каждого ciphertext, но
текущий формат не связывает запись с user/provider через associated data и не
содержит key ID/version. Master key хранится вне БД и резервируется отдельно.
Расшифрование доступно только Gmail provider path.

Access token краткоживущий и не сохраняется/логируется без необходимости. Token
не возвращается API/MCP. Реализация не содержит key ring или команды
перешифрования: замена ключа делает старые token records нечитаемыми. Текущая
безопасная процедура — pause/kill switch, отзыв Google token, замена ключа и новое
OAuth-подключение; бесшовная ротация требует отдельной реализации и миграции.
Процедура — в `gmail-oauth.md` и `operations.md`.

## Загрузка резюме

Разрешён только ожидаемый формат PDF, если конфигурация явно не расширена.
Проверяются:

- серверный limit body и отдельный `MAX_RESUME_BYTES`;
- безопасное расширение и заявленный MIME;
- magic bytes/фактический MIME;
- PDF parser в ограниченном режиме, без выполнения embedded action;
- SHA-256 всего файла;
- случайный storage key, не исходное имя;
- sanitized display filename;
- отсутствие path separators, `..`, NUL, control/bidi tricks;
- запись только внутри выделенного storage root;
- повторное чтение через storage abstraction и ID.

Оригинальное имя является metadata. LLM/MCP/Job text не выбирает storage key или
filesystem path. Download требует authz и отдаёт безопасный `Content-Disposition`,
`nosniff`; resume directory не публикуется Caddy как static.

Антивирус/Content Disarm можно добавить перед verified, если deployment этого
требует. Не помечайте файл verified только потому, что расширение `.pdf`.

## SSRF и сетевой egress

Любой outbound URL проходит валидацию при сохранении, discovery и непосредственно
перед соединением:

- scheme только разрешённый (`https` для production, редкие исключения явно);
- отсутствуют embedded credentials и ambiguous host encoding;
- hostname входит в source allowlist;
- порт входит в узкий allowlist;
- DNS resolves только в разрешённые public IP;
- запрещены loopback, private, link-local, unspecified, multicast/reserved ranges;
- запрещены IPv6 loopback/ULA/link-local и IPv4-mapped variants;
- запрещены cloud metadata IP/hostnames;
- каждый redirect проверяется заново;
- redirect count и response size ограничены;
- DNS-rebinding снижается pin/проверкой фактического peer IP;
- outbound proxy/firewall повторяет deny rules.

Запрещённые цели включают `127.0.0.0/8`, RFC1918, `169.254.0.0/16`, `::1`,
`fc00::/7`, `fe80::/10` и metadata endpoints. Список должен строиться стандартной
IP-классификацией, включая менее очевидные special-use ranges, а не только
строковыми prefix checks.

URL из vacancy description не сканируется автоматически. Официальный employer
domain/contact evidence требует отдельной проверки. HTTPX redirects не должны быть
безусловно включены до validation hook.

## Crawler safety

- ручная проверка условий/access policy до массового scan;
- явный user agent без маскировки человека;
- per-source rate/concurrency/max-depth;
- timeouts, bounded retries/backoff и `Retry-After`;
- запрет login/CAPTCHA/anti-bot bypass;
- Playwright только как bounded fallback;
- max response size/content-type;
- parser errors наблюдаемы, не замещаются фиктивным успехом;
- деградация источника останавливает downstream и массовое закрытие;
- HTML fixtures обезличены; CI не зависит от live source.

Публичная доступность не означает юридическое разрешение на массовый сбор.
Оператор обязан проверить условия и применимое право.

## Prompt injection и LLM isolation

Недоверенный текст заключён в явно маркированный data envelope. System/developer
rules и строгая output schema задаются отдельно. LLM не имеет инструментов Gmail,
filesystem, preferences, MCP или произвольного HTTP.

После LLM:

- JSON проходит Pydantic strict validation и bounded retry;
- значения score ограничены диапазоном;
- decision проверяется enum;
- facts сверяются с подтверждённым профилем/Resume;
- recipient/resume/policy/limit никогда не берутся из ответа;
- deterministic engine может только ужесточить решение;
- invalid output ведёт в `pending_review`, не auto-send.

Injection/scam indicators блокируют автоматику. Regression fixtures должны
доказывать, что текст не меняет recipient, attachment, daily limit, preferences и
не вызывает повторную отправку.

## Контакты и privacy

Используются только публично опубликованные адреса/официальные формы с evidence.
Запрещены утечки, guessed addresses и личные адреса сотрудников, не предназначенные
для откликов. Contact verification хранит official domain, confidence/status и
evidence URL.

В логах и UI показывайте минимум PII, необходимый роли. Профиль, Resume, contacts,
Application body и email delivery имеют отдельные access checks. Export/delete
выполняются по документированной retention/legal policy и не уничтожают
обязательный аудит без законного основания.

## Детерминированная отправка

Policy engine не делегирует LLM hard rules. Для auto-send нужны одновременно
deployment/user flags, отсутствие pause, allowlisted category, score threshold,
выполненные requirements, active/healthy job, verified contact/resume, validated
facts/letter, отсутствие scam/previous send/`delivery_unknown`, свободный daily
limit.

Sender принимает только Application ID и повторяет проверку непосредственно перед
Gmail call. Atomically reserved daily slot, unique idempotency key и lock защищают
от race. `delivery_unknown` не retry-ится автоматически.

Подробный контракт — `auto-send-policy.md`.

## База данных и Redis

- PostgreSQL/Redis не публикуются в интернет;
- TLS/auth применяются для внешних managed services;
- корневой Compose сейчас использует один DB credential для runtime и migration;
  production с более строгой моделью должен создать отдельные runtime/migration
  roles и выдать DDL только одноразовому migration service;
- SQLAlchemy parameterization, без конкатенации пользовательского SQL;
- DB constraints дублируют критическую идемпотентность/state integrity;
- Redis keys namespaced, locks имеют owner token/TTL;
- Redis не является источником истины для policy/send state;
- встроенный backup создаёт custom dump и checksum в host-local named volume;
  шифрование, off-host перенос, retention и restore drills настраивает оператор.

Административные SQL consoles недоступны через панель/MCP.

## Состояния и конкурентность

Изменения Application/status выполняются допустимыми переходами и атомарным
compare-and-set/transaction. Celery message несёт ID, а не доверенную копию
recipient/policy. Worker перечитывает состояние из PostgreSQL.

Full scan одного source и send одной Application защищены locks. Lock дополняет,
но не заменяет unique constraints/transaction. Истёкший lock не должен разрешить
повторную Gmail отправку, если первая имеет неопределённый результат.

## Логи, аудит и метрики

JSON log содержит correlation/scan/application ID, но не:

- Authorization/Cookie/OAuth/LLM keys;
- raw MIME, полный Resume/letter/HTML;
- provider raw response;
- произвольные query parameters;
- лишнюю PII.

AuditEvent append-oriented и фиксирует actor, action, entity, decision, sanitized
details, correlation id, timestamp. Особо важны login, OAuth connect/revoke,
profile/preferences, source changes, scans, policy, approve/send, pause/resume и
credential rotation.

Metric labels имеют низкую cardinality; не используйте email, URL, job title или
application ID как label. Доступ к `/metrics` ограничивается сетью/аутентификацией,
если endpoint раскрывает эксплуатационные детали.

В поставляемом Compose Caddy возвращает `404` для публичного `/metrics`, а сам API
endpoint предназначен для внутреннего scraper. Prometheus, Alertmanager, внешний
log sink и доставка alert-уведомлений не входят в поставку. Метрики process-local,
сбрасываются при рестарте и требуют внешней агрегации; значения, увеличенные в
Celery worker, не появляются в registry API без отдельного multiprocess/exporter
решения.

## Supply chain и CI

- зависимости фиксируются lockfile и обновляются контролируемо;
- dependency audit и минимальный SAST запускаются в CI;
- Ruff/mypy/tests/migration check/Docker build обязательны;
- secret scanner проверяет историю/изменения;
- GitHub Actions имеют минимальные permissions, third-party actions pin-ятся на
  commit SHA;
- pull request из fork не получает production secrets;
- build provenance/image digest сохраняется;
- base images регулярно обновляются и запускаются не от root;
- ненужные compilers/caches не остаются в runtime image.

Найденная CVE оценивается по достижимости и риску, но не скрывается. Исключение
audit требует владельца, срока и обоснования.

## Production host и proxy

- наружу доступны только 80/443;
- Caddy автоматически управляет TLS, private keys защищены volume permissions;
- API port не опубликован, а Caddy является единственным ingress в изолированной
  backend-сети;
- поставляемый `--forwarded-allow-ips=*` считается допустимым только при этой
  изоляции; для общей сети задаётся точный trusted proxy IP/CIDR или proxy headers
  отключаются;
- request/body/header/timeouts ограничены;
- API container не privileged, без Docker socket и лишних capabilities;
- filesystem read-only где возможно, writable только storage/tmp;
- NTP включён;
- egress deny блокирует private/metadata независимо от приложения;
- backups и storage не раздаются web server-ом.

## Vulnerability reporting и инциденты

Не публикуйте exploit/секрет в issue. Используйте приватный security contact
владельца deployment. При инциденте:

1. включить global pause и emergency email kill switch;
2. изолировать затронутый source/account/process;
3. сохранить audit/log evidence без расширения утечки;
4. отозвать/ротировать credentials;
5. проверить Gmail Sent, EmailDelivery и admin/MCP actions;
6. устранить причину и добавить regression test;
7. восстановить из проверенного backup при необходимости;
8. возобновить минимально необходимую функциональность поэтапно.

Подробные сценарии — `operations.md`.

## Security acceptance checklist

- [ ] production не стартует с development key/default password;
- [ ] HTTPS, secure cookies, CSRF и внешний distributed rate limit проверены;
- [ ] MCP bearer revoke работает; требуемое разделение ролей обеспечено gateway или
  отдельной scoped-key реализацией;
- [ ] secrets отсутствуют в Git/image/log/response;
- [ ] refresh token зашифрован; single-key ограничение принято, а
  revoke/reconnect runbook проверен;
- [ ] upload отвергает oversized/non-PDF/path traversal;
- [ ] SSRF tests покрывают IPv4/IPv6/DNS/redirect/metadata;
- [ ] external HTML escaped/sanitized;
- [ ] LLM не имеет Gmail/MCP/filesystem и invalid JSON fail closed;
- [ ] verified contact/resume/facts обязательны;
- [ ] policy/pause/limit/idempotency проверяются при send;
- [ ] `delivery_unknown` не retry-ится;
- [ ] source degradation не закрывает jobs массово;
- [ ] audit присутствует для privileged actions;
- [ ] dependency/secret/SAST checks проходят;
- [ ] backup restore и incident pause отрепетированы.
