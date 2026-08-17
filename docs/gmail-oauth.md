# Gmail OAuth 2.0

Gmail используется только для доставки уже подготовленной и разрешённой
Application. LLM, crawler, MCP-клиент и пользовательский HTTP payload не получают
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

## Минимальный scope

Для отправки нужен узкий scope:

```text
https://www.googleapis.com/auth/gmail.send
```

Не запрашивайте чтение, изменение или полный доступ к почте. Если проекту для
идентификации подключённого аккаунта требуется дополнительный OIDC scope,
документируйте необходимость отдельно; не расширяйте Gmail scope «на будущее».
Повторная аутентификация может потребоваться после изменения scope.
Callback требует наличие `gmail.send` и отклоняет ответ провайдера с любым
неожиданным дополнительным scope, не сохраняя выданный refresh token.

Следуйте актуальным требованиям Google к OAuth consent/verification для выбранного
типа пользователей. Этот документ не утверждает, что конкретная конфигурация
освобождена от проверки Google. Источник истины —
[официальный перечень Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes).

## Серверная конфигурация

Точные имена переменных находятся в `.env.example`. Обычно требуются:

- OAuth client ID;
- OAuth client secret;
- точный redirect/public base URL;
- ключ шифрования token storage;
- provider mode (`fake` в тестах, Gmail только при явном production выборе);
- независимый real-send/emergency switch.

Не передавайте refresh token через `.env`, MCP или форму вручную. Он появляется в
OAuth callback, проверяется и сохраняется зашифрованно. `.env` имеет права `0600`
либо заменяется secret manager.

## Authorization code flow

Ожидаемый безопасный flow:

1. аутентифицированный администратор вызывает защищённый endpoint подключения;
2. сервер создаёт случайный непрозрачный `state`, отдельный browser-binding cookie
   и PKCE code-verifier/challenge; в БД остаются только SHA-256 хеши `state`/binding,
   actor, срок и зашифрованный verifier;
3. браузер перенаправляется на Google authorization endpoint с минимальным scope;
4. Google возвращает `code` и `state` только на точный callback;
5. сервер одним условным `UPDATE` проверяет хеши `state`/binding, 10-минутный срок
   и отсутствие предыдущего использования, атомарно отмечает запрос использованным,
   затем расшифровывает и удаляет server-side PKCE verifier;
6. только после commit одноразового перехода сервер обменивает authorization code
   server-to-server; timeout/ошибка провайдера не разрешает повторное использование;
7. проверяет наличие выданного refresh token, не раскрывая его браузеру;
8. шифрует refresh token Fernet с текущим единственным encryption key;
9. access token остаётся короткоживущим и не логируется;
10. AuditEvent фиксирует start, success или безопасный error code без state, binding,
    verifier, authorization code и token.

При одном scope `gmail.send` текущая реализация не запрашивает OIDC identity или
Gmail profile, не сохраняет email подключённого Google account и не сверяет его с
ожидаемым адресом/tenant. Ошибочно выбранный аккаунт поэтому не будет обнаружен
сервером до отправки от `userId="me"`. Оператор обязан проверить выбранный аккаунт
на экране Google consent и выполнить контролируемую staging-проверку. Для
server-side identity enforcement сначала добавьте обоснованные `openid`/`email`
claims либо разрешённый userinfo-механизм, настройте allowlist и проверку claims,
затем заново проведите OAuth/security review; такая проверка сейчас не реализована.

Для получения refresh token обычно требуется offline access; поведение выдачи
refresh token зависит от уже выданного consent. Не пытайтесь извлекать его из
браузерных storage или просить пользователя вставить его в чат.

Запрашивайте `access_type=offline`; принудительный `prompt=consent` применяйте
только при первоначальном consent/явном переподключении, а не при каждом входе.
Точные параметры и ограничения сверяйте с
[Google OAuth для web-server applications](https://developers.google.com/identity/protocols/oauth2/web-server).

Callback принимает только ожидаемый method/query, ограничивает частоту и не
показывает provider error с чувствительными деталями. OAuth code одноразовый.
Browser-binding хранится в `HttpOnly`, `SameSite=Lax`, host-only cookie, ограниченном
путём callback; в HTTPS deployment также установлен `Secure`. Callback очищает cookie
после успеха и после ошибки. Утерянный cookie требует начать новый OAuth flow.

## REST lifecycle

Все управляющие endpoints, кроме самого Google callback, требуют настроенный Bearer
API credential:

- `GET /api/v1/oauth/gmail/start` — создаёт одноразовую server-side запись, ставит
  binding cookie и возвращает redirect на Google;
- `GET /api/v1/oauth/gmail/callback` — browser callback Google; самостоятельно не
  принимает recipient, письмо, вложение или refresh token;
- `GET /api/v1/oauth/gmail/status` — возвращает только configured/connected, точный
  scope, timestamps, число незавершённых flow и явный `identity_verified=false`;
- `DELETE /api/v1/oauth/gmail` — удаляет локальный encrypted refresh token и
  инвалидирует все незавершённые authorization requests.

`DELETE` является локальным disconnect, а не подтверждением отзыва grant у Google;
ответ явно содержит `remote_grant_revoked=false`. Для полного отзыва оператор также
удаляет доступ приложения в Google Account security controls. Endpoint и callback не
возвращают client secret, ciphertext, OAuth code, `state`, verifier или binding.

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

Retry ограничен по количеству, использует exponential backoff+jitter и применяется
только когда можно безопасно утверждать, что Gmail не принял сообщение, либо
provider даёт однозначно retryable ответ.

- 429/некоторые 5xx: уважать `Retry-After`, ограниченно повторять;
- refresh access token: выполнять внутри provider, refresh token не логировать;
- revoked/invalid grant: остановить отправку и потребовать reconnect;
- invalid recipient/payload: `failed`, не бесконечный retry;
- timeout/connection loss после возможной передачи: `delivery_unknown`.

`delivery_unknown` автоматически не повторяется. Оператор сверяет Gmail Sent и
сохранённые IDs. Это важнее риска пропустить одно письмо, чем отправить дубль.

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
2. Прогоните полный E2E с fake provider.
3. Создайте отдельный staging OAuth client/test user.
4. Пройдите consent через защищённую панель.
5. Проверьте audit и зашифрованное хранение без вывода ciphertext/token.
6. Если предусмотрен dry-run, проверьте MIME без provider send; не добавляйте
   произвольный recipient.
7. Выполните единичную контролируемую отправку только на явно разрешённый тестовый
   адрес после отдельного operator review.
8. Удалите тестовые Application и токены согласно retention, не подменяя audit.

Автоматические тесты никогда не используют реальный Gmail.

## Production-включение

1. Настройте отдельный production OAuth client и HTTPS callback.
2. Проверьте consent/verification requirements Google.
3. На экране Google consent вручную проверьте ожидаемый аккаунт; сервер пока не
   сохраняет и не валидирует его email/tenant.
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
единственную Gmail credential и её timestamps; отдельной key version в записи нет.

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

- [ ] scope ограничен `gmail.send`;
- [ ] callback — точный HTTPS URL;
- [ ] state непрозрачный и короткоживущий, в БД хранится только его хеш;
- [ ] state атомарно одноразовый, привязан к browser cookie/actor, PKCE verifier
  зашифрован server-side и удаляется до token exchange;
- [ ] token не появляется в браузере/API/MCP/логах;
- [ ] refresh token защищён Fernet, единственный ключ находится вне БД;
- [ ] выбранный Google account проверен оператором; server-side identity check
  пока отсутствует;
- [ ] sender принимает только application ID;
- [ ] recipient и Resume выбирает сервер;
- [ ] финальная policy/limit/pause проверяется перед вызовом;
- [ ] двойной запуск идемпотентен;
- [ ] `delivery_unknown` не retry-ится;
- [ ] CI/E2E использует fake provider;
- [ ] real send по умолчанию выключен;
- [ ] revoke/reconnect и incident runbook проверены.
