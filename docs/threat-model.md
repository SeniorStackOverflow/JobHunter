# Модель угроз

## Область и цель

Модель охватывает `job-agent`: FastAPI/admin/MCP, Celery/Beat, crawler adapters,
PostgreSQL, Redis, storage резюме, LLM providers, Gmail OAuth/API, reverse proxy и
операционные процессы.

Цели безопасности:

1. никто не отправляет письмо без явной пользовательской политики и hard rules;
2. одна логическая Application не отправляется дважды;
3. недоверенный сайт/LLM не получает secrets и не управляет системой;
4. crawler не становится SSRF-прокси во внутреннюю сеть;
5. профиль, Resume, контакты и OAuth-токены сохраняют конфиденциальность;
6. история вакансий, решений, отправок и аудит сохраняют целостность;
7. деградация источника не вызывает массовых опасных действий;
8. сервис остаётся управляемым через pause/disable/recovery.

Модель пересматривается при добавлении источника/provider, изменении auth,
включении новой формы отправки, расширении scope или крупном изменении deployment.

## Активы

- Gmail refresh/access token и OAuth client secret;
- право отправлять письмо от имени пользователя;
- профиль, подтверждённые факты и preferences;
- PDF-резюме и storage keys;
- EmployerContact и Application content;
- SourceJob/CanonicalJob/Snapshots/MatchEvaluation;
- policy versions, idempotency keys и delivery status;
- admin sessions, MCP credentials и encryption keys;
- PostgreSQL/backup/audit log;
- availability worker/scheduler/source capacity;
- репутация пользователя и соблюдение условий источников.

## Субъекты угроз

- злоумышленник, публикующий вакансию с prompt injection/scam;
- контролируемый им сайт/redirect/DNS;
- внешний неаутентифицированный web/MCP-клиент;
- пользователь с отозванным/невалидным token или с ролью внешнего gateway,
  пытающийся повысить права;
- скомпрометированный admin/MCP credential;
- недобросовестный или ошибающийся оператор;
- уязвимая/скомпрометированная зависимость или CI action;
- случайный отказ Gmail/LLM/job board/сети;
- изменение HTML/access policy без злого умысла;
- другой tenant/процесс на общем хосте, если deployment не изолирован.

Google, LLM provider, облачная платформа и job boards считаются внешними
зависимостями, а не полностью доверенными внутренними компонентами.

## Предположения

- production host, DNS и secret manager администрируются компетентным оператором;
- пользователь имеет право применять загруженные Resume и подключённый Gmail;
- сервер синхронизирует время;
- TLS private key не скомпрометирован;
- PostgreSQL обеспечивает требуемые транзакции/constraints;
- публичная доступность данных не заменяет юридическую проверку их использования;
- система одно-пользовательская либо tenant isolation добавлена до multi-user
  эксплуатации.

Нарушение предположения требует отдельной архитектурной проверки.

## Потоки и границы

```text
Internet job sources (untrusted)
       │ HTML/API/URLs
       ▼
Crawler boundary ──► normalized SourceJob ──► DB
                                             │
Profile/Resume ──► matching ──► LLM boundary │
                         │ validated result  │
                         ▼                   │
verified contact ─► application ─► policy ──┤
                                             ▼
Admin / MCP ─authn/authz/audit─► services ─► Celery/Redis
                                             │ application_id
                                             ▼
                                      Gmail provider ─► Google
```

Границы пересекают также OAuth browser redirect, upload PDF, Caddy, backup и CI.

## Анализ угроз

| ID | Угроза | Последствие | Основные меры | Остаточный риск |
|---|---|---|---|---|
| T1 | Prompt injection в вакансии | изменение поведения, утечка, неверная отправка | data envelope, LLM без tools/secrets, strict schema, deterministic policy, injection tests | модель может не распознать новую формулировку; hard rules ограничивают impact |
| T2 | Scam/поддельный контакт | письмо мошеннику, утечка Resume | verified public contact, official-domain/evidence, scam rules, review | официальный домен/страница тоже могут быть скомпрометированы |
| T3 | SSRF через URL/redirect | metadata/internal service access | allowlist, DNS/IP/peer validation, redirect checks, egress firewall | сложные DNS/proxy ошибки требуют постоянных тестов |
| T4 | Path traversal/вредный PDF | чтение/запись файла, parser exploit | random storage key, root containment, MIME/magic/size, no execution, optional AV | zero-day PDF parser/storage layer |
| T5 | Кража OAuth token | отправка от имени пользователя | minimal gmail.send, authenticated encryption одним key вне БД, redaction, revoke | компрометация runtime с decrypt key позволяет использование; key ring отсутствует |
| T6 | Повторная отправка/race | репутационный ущерб | CanonicalJob uniqueness, idempotency key, DB CAS, locks, delivery record | timeout после provider acceptance требует manual reconciliation |
| T7 | Превышение дневного лимита | массовая отправка | atomic slot reservation, sending/unknown учитываются, final check | неверный timezone/config; нужен audit/alert |
| T8 | Подмена recipient/attachment | утечка/неверное письмо | sender принимает только application_id, server loads verified records | компрометация БД/admin остаётся высокорисковой |
| T9 | Broken adapter массово закрывает jobs | потеря вакансий/ошибочные решения | degradation circuit breaker, absence threshold, snapshots, no mass close | медленный частичный drift может обойти baseline |
| T10 | Обход auth/CSRF | изменение политики/отправка | admin session, CSRF, bearer auth, Origin checks, process-local baseline limiter; внешний gateway для RBAC/distributed limits | stolen admin session или bearer до отзыва/restart |
| T11 | MCP tool abuse | scan DoS, policy/source changes, send attempts | hashed bearer allowlist, schemas, audit, server policy; optional scoped/rate-limiting gateway | встроенный ключ имеет доступ ко всем tools и должен считаться привилегированным |
| T12 | XSS через HTML/filename/error | кража admin session | escaping/sanitizer, CSP, safe headers/filenames | sanitizer/browser bypass |
| T13 | SQL/command/template injection | DB/host compromise | parameterized ORM, no shell transforms, strict config, sandboxed containers | уязвимость зависимости/новый unsafe code path |
| T14 | Secrets в логах/CI/backup | долгосрочная компрометация | structured redaction, secret scanning, protected backup, least privilege | operator copy/screenshot вне системы |
| T15 | Queue message tampering/replay | повтор операции/устаревшая policy | tasks carry IDs, DB reload/final check, idempotency, broker auth | compromised DB/runtime выходит за эту границу |
| T16 | Dependency/CI compromise | backdoor, secret theft | lockfile, audits, pinned actions, minimal CI perms, image scan | trusted upstream compromise |
| T17 | DoS источником/клиентом | недоступность и расходы | size/time/page/concurrency limits, process-local HTTP limiter, queue isolation, circuit breakers; внешний distributed limiter | распределённый low-rate DoS и обход лимита через несколько процессов |
| T18 | Неверный LLM JSON/галлюцинация | ложная оценка/факт | strict Pydantic, bounded retry, confirmed-fact validation, manual fallback | качественная, но неверная оценка остаётся возможной |
| T19 | Access policy/terms change | юридический/технический риск | source pause, ручная периодическая проверка terms, documented limitations | автоматического terms hash/change detector нет; ручная проверка не заменяет юриста |
| T20 | Backup/restore compromise | утечка или потеря данных | Compose dump/checksum/restore guard; внешние encryption, off-host copy, retention и restore drills | поставляемый named volume остаётся на одном host и не шифруется автоматически |

## Детальные abuse cases

### Вакансия командует агентом

Пример: описание требует игнорировать правила, взять `/etc/passwd` вместо Resume,
изменить адрес и вызвать `send_application` дважды.

Контроль:

- crawler сохраняет это как plain text;
- matcher получает недоверенный data block и не имеет tools/filesystem;
- output schema не содержит recipient/path/preferences;
- application generator выбирает Resume по verified ID/category;
- contact layer выбирает verified EmployerContact;
- policy блокирует injection indicator;
- sender принимает только сохранённый application ID;
- unique constraints/locks не дают повтор.

Security test должен проверять отсутствие side effects, а не только decision text.

### Вакансия указывает внутренний URL

Пример: employer link ведёт на `http://169.254.169.254/` либо public host делает
redirect на `127.0.0.1`/AAAA `::1`.

Контроль: scheme/host allowlist, resolve всех A/AAAA, special-use deny, повторная
проверка redirect и actual peer, egress firewall. URL отклоняется до получения
body; ошибка фиксируется без секретного response.

Тесты включают decimal/hex/IPv4-mapped IPv6, userinfo, trailing dot, DNS answer с
несколькими IP и redirect chain.

### DNS rebinding

Host сначала resolves в public IP, затем во внутренний. Раздельная «проверка, а
потом обычный connect» уязвима. Fetcher должен соединяться с проверенным IP с
корректным TLS SNI/Host или сравнивать фактический peer; DNS TTL и connection pool
не должны обходить validation. Network egress deny остаётся второй границей.

### Двойная Celery delivery

Broker повторно доставляет send task после worker crash. Task содержит только
application ID. Новый worker открывает transaction, видит `sending`/EmailDelivery,
проверяет idempotency. Если неизвестно, был ли provider вызван, переходит/оставляет
`delivery_unknown`, не отправляет повторно.

### Смена политики после постановки в очередь

Пользователь включает global pause или запрещает категорию после `auto_approved`.
Worker перечитывает текущие preferences/emergency switch и пересчитывает hard
rules перед Gmail call. Старое решение не является capability token.

### HTML drift выглядит как массовое закрытие

Recheck parser перестал видеть активный marker, многие страницы одновременно
«пропали». Health evaluator замечает correlated anomaly/parsing errors и включает
circuit breaker. Absence counters не увеличиваются; источник paused/degraded,
другие продолжают работу.

### Компрометированный MCP key

Встроенный bearer key является привилегированным и позволяет вызывать write tools;
server-side policy по-прежнему запрещает произвольного получателя, вложение и обход
лимитов, но атакующий может менять профиль/источники и запускать допустимые операции.
Process-local rate limit, audit и удаление hash из конфигурации с перезапуском API
ограничивают ущерб. Для разных уровней доступа или распределённых лимитов
обязателен scoped identity gateway либо отдельная scoped-key реализация.

### Злонамеренный администратор

Технически привилегированный оператор может изменить preferences/contact state или
получить Resume. Меры: минимальные роли, MFA/identity provider, append-oriented
audit, разделение deployment secret и app admin, alerts на включение real-send и
смену contact/policy. Полностью устранить риск без организационного контроля и
multi-party approval нельзя.

## STRIDE по компонентам

### API/admin/MCP

- Spoofing: credential theft → secure session, hashed bearer allowlist,
  operator-managed revoke/restart; встроенных bearer scopes нет.
- Tampering: CSRF/parameter manipulation → CSRF, schemas, audit; RBAC требует
  внешнего gateway или новой серверной модели.
- Repudiation: отрицание approve/send → actor/correlation AuditEvent.
- Information disclosure: over-broad responses → field-level minimization/redaction.
- Denial of service: scans/large uploads → rate/body/task limits.
- Elevation: якобы read-only key вызывает write → любой встроенный bearer уже
  имеет все tools; реальное разделение обеспечивается только gateway/scoped-key
  расширением.

### Crawler

- Spoofing: fake employer/source → configured source identity/TLS/allowlist.
- Tampering: HTML/structured payload → strict parser/normalization/snapshots.
- Repudiation: источник меняет content → hash/snapshot/timestamps, не юридическая
  non-repudiation.
- Disclosure: SSRF → IP/redirect/egress controls.
- DoS: infinite pagination/huge body → max pages/body/time/concurrency.
- Elevation: YAML transform executes code → declarative allowlisted transforms.

### LLM/application/policy

- Spoofing/tampering: injection/invalid JSON → isolation/schema/confirmed facts.
- Repudiation: модель/правила неизвестны → model, prompt/rules/policy version.
- Disclosure: excess PII in prompt → minimization, no secrets/full Resume unless
  strictly needed and approved provider policy.
- DoS: oversized vacancy/retries → truncation/token/retry bounds.
- Elevation: LLM decision becomes send → deterministic hard rules.

### Gmail

- Spoofing: stolen token → AEAD/minimal scope/revoke.
- Tampering: recipient/MIME arguments → application-ID-only boundary.
- Repudiation: unknown provider result → EmailDelivery/message id/audit.
- Disclosure: wrong attachment/log → server selection, SHA-256, redaction.
- DoS: quota/429/revocation → bounded backoff/alert/reconnect.
- Elevation: queue task skips policy → final reload/check/transaction.

### Storage/DB/Redis/backup

- Spoofing: service credentials → network isolation/unique credentials.
- Tampering: direct DB/queue → constraints, least privilege, DB-as-truth.
- Repudiation: audit modification → append-oriented schema; отдельная DB role и
  внешний immutable log sink не входят в текущий Compose и добавляются при
  требуемом уровне защиты.
- Disclosure: volume/backup theft → host access controls; encryption/off-host
  retention должен настроить оператор.
- DoS: disk/queue exhaustion → quotas/alerts/backpressure.
- Elevation: migration/runtime credential слишком широк → поставляемый Compose не
  разделяет эти DB roles; production deployment должен разделить их до расширения
  круга операторов/тенантов.

## Privacy threats

Даже без классической атаки возможны:

- отправка чрезмерного объёма профиля LLM/provider;
- хранение вакансий/контактов/резюме дольше цели;
- раскрытие PII в daily report/metrics;
- межпользовательское смешение данных при будущем multi-tenant режиме;
- использование публичного личного email вне цели публикации.

Меры: data minimization, purpose limitation, field-level auth, retention/delete
workflow, no PII metric labels, tenant key во всех constraints/queries до
multi-user запуска, review contact purpose/evidence.

## Availability и safe failure

Компонент должен отказывать безопасно:

- PostgreSQL недоступен → нет policy/send; health not ready;
- Redis недоступен → scheduler/queue остановлены, прямой обход locks запрещён;
- LLM invalid/unavailable → pending review, не auto-send;
- Gmail timeout ambiguous → delivery unknown, no retry;
- source degraded → source pause, no mass close/applications;
- encryption key недоступен → Gmail disabled, token не удаляется;
- audit write failed для privileged action → операция fail closed там, где это
  предусмотрено требованиями целостности.

## Остаточные риски и ограничения

- Ни HTML parser, ни LLM не гарантируют распознавание всех scam/injection.
- Official website/contact может быть скомпрометирован.
- `gmail.send` всё ещё позволяет отправлять почту; компрометация runtime вместе с
  ключом расшифрования критична.
- Provider acceptance не гарантирует доставку/прочтение; `sent` означает
  подтверждённый API result, не человеческую доставку.
- Semantic dedup может ошибочно объединить или разделить вакансии; merge обратим,
  но неверный merge может блокировать второй отклик.
- Условия сайта и право меняются; техническая реализация не является юридическим
  заключением.
- Single-admin deployment имеет высокий insider risk.
- Docker/host root может читать процессы, volumes и secrets.
- Backup с PII остаётся чувствительным даже после удаления live-записи до истечения
  retention.

## Проверка контролей

Автоматические тесты:

- SSRF IPv4/IPv6/redirect/DNS/metadata;
- upload size/MIME/magic/path traversal;
- HTML escaping и CSRF/authz;
- strict LLM JSON и bounded fallback;
- prompt injection side-effect assertions;
- verified facts/contact/resume;
- policy rules, pause, emergency switch, daily limit;
- concurrent idempotency и duplicate canonical job;
- `delivery_unknown` no retry;
- recheck transition/degradation circuit breaker;
- MCP bearer revoke/audit и, при необходимости, role separation во внешнем gateway;
- fake Gmail only в CI.

Ручные/операционные проверки:

- OAuth consent/scope/redirect и revoke;
- egress firewall/metadata denial из контейнера;
- reverse proxy headers/streaming/origin;
- restore drill и encryption key recovery;
- emergency pause во время queued send;
- source policy review/live opt-in smoke;
- secret leak exercise и credential rotation;
- audit review/alert delivery.

## Регистр риска

Владелец deployment ведёт risk register с полями: ID, описание, актив,
вероятность/impact, controls, test evidence, owner, due date, accepted residual
risk и дата пересмотра. High-impact исключение без владельца и срока не допускается.

После инцидента или существенного изменения обновляются одновременно threat model,
security tests и runbook, а не только текст документа.
