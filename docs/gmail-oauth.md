# Gmail OAuth 2.0

Актуализировано 2026-09-30. Gmail используется для доставки подготовленной и
разрешённой Application и read-only сверки DSN/bounce/ответов. LLM, crawler, MCP-клиент и пользовательский HTTP payload не получают
access/refresh token и не формируют raw MIME.

Реальные credentials не нужны для разработки и CI: там используется fake Gmail
provider с тем же контрактом и реальной проверкой статусов/idempotency.

## Граница sender

Публичный интерфейс sender принимает только:

```text
application_id
```

Сервер по этому ID загружает и повторно проверяет:

- Application и актуальный policy decision;
- verified EmployerContact/recipient;
- subject/body из сохранённой Application;
- selected verified Resume и его storage key/hash;
- отсутствие предыдущей отправки/`delivery_unknown`;
- дневной лимит, pause и emergency switch;
- idempotency key.

Запрещены параметры recipient, arbitrary MIME, attachment path или произвольный
файл. Это правило действует для REST, MCP, Celery task и внутренних вызовов.

## Google Cloud project

В отдельном Google Cloud project:

1. включите Gmail API;
2. настройте OAuth consent screen и применимый тип публикации;
3. добавьте только необходимых test users, пока приложение в testing;
4. создайте OAuth 2.0 client подходящего типа для server-side web flow;
5. добавьте точный HTTPS redirect URI production deployment;
6. храните client secret в secret manager, не в репозитории.

Redirect URI должен полностью совпадать по scheme/host/path с callback, который
показывает конфигурация `job-agent`. Не копируйте примерный домен из документа.
Для локальной разработки используйте отдельный OAuth client и только разрешённый
localhost callback; production client не должен разрешать лишние origins/redirects.

## Вход и минимальные scopes

Обычный вход `/auth/google/login` и регистрация `/auth/google/register`
используют отдельный `GoogleIdentityService`: только `openid` и `email`.
Они не запрашивают Gmail consent и не заменяют сохранённую почтовую credential.
Тот же callback `/api/v1/oauth/gmail/callback` различает provider/actor
одноразового запроса. `/admin/auth/google` — legacy redirect на общий вход.

Gmail подключается отдельным действием из настроек. Для доставки и мониторинга:

```text
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.readonly
```

`gmail.readonly` нужен reconciler для DSN/bounce и ответов в известных threads;
он не разрешает менять или удалять письма. API-only подключение с двумя Gmail
scopes не доказывает identity mailbox. Legacy операторский connect path может
также запросить `openid`/`email` для проверки admin allowlist; это отдельное явное
действие подключения, а не обычный вход.

Текущие browser routes и хранение сессий описаны в
[accounts-panel.md](accounts-panel.md) и [google-oauth-sessions.md](google-oauth-sessions.md).
Список scopes Google: [официальная документация](https://developers.google.com/workspace/gmail/api/auth/scopes).

## Серверная конфигурация

Точные имена переменных находятся в `.env.example`. Обычно требуются:

- OAuth client ID;
- OAuth client secret;
- точный JSON allowlist `GOOGLE_ADMIN_EMAILS`;
- точный redirect/public base URL;
- ключ шифрования token storage;
- provider mode (`fake` в тестах, Gmail только при явном production выборе);
- независимый real-send/emergency switch.

Не передавайте refresh token через `.env`, MCP или форму вручную. Он появляется в
OAuth callback, проверяется и сохраняется зашифрованно. `.env` имеет права `0600`
либо заменяется secret manager.

## Authorization code flow

Общий механизм для identity и Gmail flows:

1. Сервер создаёт случайные state и browser binding, server-side PKCE verifier,
   actor и account binding. В БД сохраняются хеши state/binding и зашифрованный
   verifier; срок запроса — 10 минут.
2. Браузер направляется на Google; callback принимает code/state только на
   настроенном URL и с правильным binding cookie.
3. Условный UPDATE атомарно погашает state и очищает verifier до обмена code.
   Сбой provider не позволяет повторно использовать уже погашенный запрос.
4. Identity flow проверяет подпись, issuer, audience, expiry, nonce и verified
   email ID token; затем определяет admin либо активный зарегистрированный user.
   Gmail flow проверяет account/actor и необходимые Gmail scopes.
5. Gmail refresh token сохраняется зашифрованно в credential соответствующего
   аккаунта. Обновление не затрагивает Gmail другого владельца.
6. Audit хранит результат и безопасный error code; code, state, verifier,
   binding, tokens и ciphertext не возвращаются и не логируются.

Binding cookie — HttpOnly/SameSite=Lax, ограничен callback path и Secure при
HTTPS. Callback очищает cookie. Утраченный binding требует нового flow.
Gmail connect намеренно запрашивает offline consent; обычный identity login не
должен выпускать новый Gmail token. Источник параметров — `app/email/oauth.py`
и `app/auth/google.py`.

## REST и browser lifecycle

API endpoints требуют операторский Bearer credential, кроме callback:

- `GET /api/v1/oauth/gmail/start` — отдельное Gmail authorization;
- `GET /api/v1/oauth/gmail/callback` — общий callback identity/Gmail;
- `GET /api/v1/oauth/gmail/status` — safe status bootstrap admin credential;
- `DELETE /api/v1/oauth/gmail` — local disconnect bootstrap admin credential и
  незавершённых Gmail requests. Это не tenant API с произвольным account ID.

Пользователь подключает почту через `GET /app/gmail/connect` после проверки
активного account session; `POST /app/gmail/disconnect` требует session-bound
CSRF. Owner/account ID определяется сервером. Администратор отключает свою
credential через `POST /admin/oauth/gmail/disconnect` с CSRF. Существующий
операторский consent path доступен через authenticated API start, но текущая UI
ссылка `/admin/auth/google?consent=1` перенаправляет в identity login без
сохранения `consent`. Поэтому кнопка в admin settings сейчас не запускает Gmail
consent; её нельзя использовать как доказательство успешного reconnect. Это
известное ограничение runtime, требующее отдельной исправляющей правки.
Для чужого владельца UI направляет оператора к аккаунтам, не подменяет его Gmail.

Local disconnect не отзывает grant у Google. Для полного отзыва владелец удаляет
доступ приложения в Google Account. Logout из JobHunter не disconnect Gmail.
Ни один endpoint не принимает recipient, MIME или произвольный refresh token.

## Хранение токенов

Refresh token хранится только в зашифрованном поле БД:

- Fernet обеспечивает конфиденциальность и проверку целостности;
- каждый ciphertext использует собственную случайность;
- ключ производен от одного `TOKEN_ENCRYPTION_KEY`;
- associated data, key ID/version и key ring отсутствуют;
- master key находится вне БД/backup;
- расшифрование доступно только Gmail provider process path.

Нельзя хешировать refresh token вместо шифрования: для обновления access token
нужно исходное значение. Не возвращайте ciphertext через API/MCP: он всё равно
является чувствительным материалом.

## Формирование сообщения

После финальной policy-проверки сервер:

1. берёт recipient из verified EmployerContact;
2. берёт subject/body из Application;
3. читает только выбранный Resume через безопасный storage abstraction;
4. проверяет active/verified, SHA-256, размер и MIME;
5. формирует MIME локально с безопасным filename;
6. кодирует сообщение согласно Gmail API;
7. вызывает `users.messages.send(userId="me", body={"raw": ...})` с ограниченными
   timeouts/retries;
8. сохраняет Gmail message id/thread id и sanitized response.

Путь файла никогда не формируется из текста вакансии, оригинального filename или
MCP payload. Полный MIME/резюме не попадает в лог.

Формат base64url MIME и метод отправки описаны в
[официальном руководстве Gmail](https://developers.google.com/workspace/gmail/api/guides/sending).

## Idempotency и конкурентность

Gmail API не заменяет идемпотентность домена. Перед provider call выполняются:

- атомарный compare-and-set статуса Application;
- distributed/database lock по application ID;
- уникальное ограничение idempotency key/EmailDelivery attempt;
- проверка отправок CanonicalJob;
- резервирование дневного лимита.

Celery redelivery и двойной клик видят существующую попытку и не отправляют второе
сообщение. Успешный response сохраняется до освобождения lock.

## Ошибки и retry

Retry ограничен по количеству и использует задержки 15 минут, 1 час и 6 часов.
Он допустим при однозначной временной ошибке provider или подтверждённом
временном DSN; при неизвестном исходе отправки повтор запрещён.

- 429/некоторые 5xx: уважать `Retry-After`, ограниченно повторять;
- refresh access token: выполнять внутри provider, refresh token не логировать;
- permanent refresh rejection (revoked/invalid grant): потребовать reconnect
  только у затронутого аккаунта; temporary refresh failures допускают bounded
  retry, не помечают grant отозванным и не продвигают mailbox cursor;
- invalid recipient/payload: `failed`, не бесконечный retry;
- timeout/connection loss после возможной передачи: `delivery_unknown`.

`delivery_unknown` автоматически не повторяется. Оператор сверяет Gmail Sent и
сохранённые IDs. Это важнее риска пропустить одно письмо, чем отправить дубль.

После provider acceptance reconciler каждые пять минут читает Gmail history с
durable cursor. Он сначала связывает DSN по исходному RFC Message-ID, затем допускает
только однозначный thread или recipient/time fallback. Permanent bounce инвалидирует
конкретный `EmployerContact`, сохраняет факт первоначальной отправки и освобождает
application slot работодателя; он не suppress-ит работодателя. Transient bounce
получает ограниченный retry schedule, причём каждый retry повторно проверяет employer
relationship policy. Обычный входящий ответ в известном thread создаёт
`employer_replied` и замораживает новые отклики этому работодателю.

Существующий token, выданный только с `gmail.send`, показывает
`delivery_ready=false`/`monitoring_ready=false` и останавливает новые отправки до
повторного OAuth consent. Без `gmail.readonly` задача безопасно завершается без
чтения mailbox, а sender не создаёт письма без обязательного post-send monitoring.

Read-only исторический отчёт запускается через `email-delivery-audit`; он не меняет
application, delivery или contact. Employer backfill сначала запускайте командами
`employer-identity-audit` и `employer-relationship-audit`, затем отдельно
`employer-backfill --apply` и `employer-remediate --apply` после проверки dry-run.

## Данные EmailDelivery

Храните:

- application ID и provider;
- recipient, уже разрешённый contact policy;
- provider message/thread ID;
- статус и timestamps;
- номер логической попытки/idempotency key;
- sanitized error/response без токенов и полного message body.

Не считайте наличие Celery success достаточным доказательством доставки; доменный
статус опирается на provider result.

## Подключение в staging

1. Оставьте server-side real send выключенным.
2. Прогоните локальный браузерный roundtrip с fake token exchange. Он проверяет
   `Secure` cookie, state и callback, но не заменяет настоящий Google consent.
3. Создайте отдельный staging OAuth client и тестовый Google account с точным HTTPS
   callback. Не используйте production client или mailbox.
4. В чистом Playwright browser context пройдите реальный Google consent, callback и
   вход в панель. Проверьте, что выданы оба Gmail scope, `delivery_ready=true`,
   `monitoring_ready=true`, а read-only mailbox audit может получить профиль и
   сообщения. Не включайте отправку писем.
5. Отключите локальное staging-подключение и повторите пункт 4 ещё два раза в
   новых чистых browser contexts. Все три последовательных прохода должны
   завершиться успешно.
6. Проверьте audit и зашифрованное хранение без вывода ciphertext/token. Тестовые
   Application и токены удаляйте согласно retention, сохраняя audit trail.

Автоматические тесты никогда не используют реальный Gmail.

## Production-включение

1. Настройте отдельный production OAuth client и HTTPS callback.
2. Проверьте consent/verification requirements Google.
3. На экране Google consent вручную проверьте ожидаемый аккаунт; при admin login
   сервер проверяет подписанный ID token и точное совпадение email с allowlist.
   После входа отдельно подключите Gmail по соответствующему connect flow
   (для admin UI учитывайте описанное ограничение); одна identity-сессия не
   предоставляет доступ к отправке или чтению почты.
4. Настройте verified resumes/contacts и консервативную policy.
5. Проверьте глобальную паузу.
6. Явно включите real Gmail provider/server-side switch.
7. Явно включите пользовательский auto-send только после проверки по
   `auto-send-policy.md`.
8. Контролируйте первую отправку и дневной отчёт.

## Отзыв и переподключение

Для отключения:

1. поставьте глобальную паузу;
2. отключите real-send switch;
3. вызовите `DELETE /api/v1/oauth/gmail` с privileged Bearer credential; это удалит
   локальный token и незавершённые OAuth requests, но не удалённый Google grant;
4. отзовите доступ приложения в Google account/security controls;
5. зафиксируйте AuditEvent;
6. проверьте queued/sending/delivery_unknown Applications.

При reconnect не меняйте исторические EmailDelivery. Текущая схема обновляет
Gmail credential соответствующего аккаунта и её timestamps; отдельной key version
в записи нет. Reconnect state и durable mailbox cursor также изолированы по владельцу.

## Ротация encryption key

Текущая реализация не имеет key ring, key ID или команды re-encryption. Простая
замена `TOKEN_ENCRYPTION_KEY` делает существующий refresh token нечитаемым.

Штатная процедура при компрометации или обязательной замене:

1. включить global pause и server-side real-send kill switch;
2. отозвать доступ приложения в Google account;
3. сохранить необходимый audit и проверенный backup отдельно от ключа;
4. заменить `TOKEN_ENCRYPTION_KEY` в secret manager и перезапустить приложение;
5. заново пройти OAuth с проверкой выбранного аккаунта;
6. выполнить refresh/dry-run или контролируемую staging-проверку без
   автоматической массовой отправки;
7. снять ограничения только после проверки.

Бесшовная ротация возможна лишь после отдельной реализации versioned ciphertext,
key ring и транзакционной миграции; этот runbook не утверждает, что они уже есть.

## Проверочный checklist

- [ ] scope ограничен `gmail.send` и `gmail.readonly`;
- [ ] callback — точный HTTPS URL;
- [ ] state непрозрачный и короткоживущий, в БД хранится только его хеш;
- [ ] state атомарно одноразовый, привязан к browser cookie/actor, PKCE verifier
  зашифрован server-side и удаляется до token exchange;
- [ ] token не появляется в браузере/API/MCP/логах;
- [ ] refresh token защищён Fernet, единственный ключ находится вне БД;
- [ ] выбранный Google account проверен оператором; admin login подтвердил
  подписанный ID token и email allowlist;
- [ ] sender принимает только application ID;
- [ ] recipient и Resume выбирает сервер;
- [ ] финальная policy/limit/pause проверяется перед вызовом;
- [ ] двойной запуск идемпотентен;
- [ ] `delivery_unknown` не retry-ится;
- [ ] CI использует fake provider; реальный staging OAuth прошёл три раза в чистых
  Playwright contexts без отправки писем;
- [ ] real send по умолчанию выключен;
- [ ] revoke/reconnect и incident runbook проверены.
