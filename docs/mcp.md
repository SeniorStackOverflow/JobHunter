# MCP Streamable HTTP

`job-agent` предоставляет удалённый MCP endpoint по `/mcp`. MCP — интерфейс
управления и чтения, а не планировщик: Celery Beat/worker продолжает работу без
подключённого AI-клиента.

Production URL имеет вид:

```text
https://job-agent.example/mcp
```

Замените домен на свой. Plain HTTP допустим только для изолированной локальной
разработки. Сервер использует Streamable HTTP; не настраивайте его как устаревший
SSE endpoint или локальный stdio command.

## Безопасность подключения

- каждый production-запрос проходит HTTPS;
- клиент передаёт выделенный bearer token в `Authorization`;
- серверная конфигурация хранит список SHA-256 hash API keys, не исходные
  значения;
- raw key создаётся и передаётся оператором вне приложения; БД не хранит для него
  владельца, роль, scope или отдельный revoke record;
- все tools, включая write tools, требуют валидный bearer key;
- приложение проверяет допустимый `Origin`, а reverse proxy сохраняет необходимые
  MCP headers;
- secrets, OAuth tokens и полный текст резюме никогда не возвращаются;
- каждый write вызов создаёт AuditEvent с actor/correlation id;
- HTTP middleware задаёт только общий process-local rate-limit baseline.

Встроенный self-hosted режим использует один уровень полномочий: каждый валидный
MCP key может видеть и вызывать все опубликованные tools, хотя policy engine всё
равно не позволяет обойти правила отправки. Если нужны роли
`job-agent.read`/`job-agent.write`/`job-agent.autosend`, поставьте перед endpoint
OIDC/OAuth-aware gateway либо расширьте сервер моделью scoped keys. Не называйте
встроенный ключ read-only: такой семантики в текущей схеме нет.

Тот же hash allowlist используется bearer-аутентификацией REST API; это не
отдельная роль. Административная HTML-панель использует собственную session auth.
Отзыв bearer выполняется удалением hash из конфигурации и перезапуском API. Один
ключ можно заменить с коротким overlap двух hash, но приложение не ведёт историю
выдачи/отзыва.

Текущий limiter не распределён между API-процессами, не считает запросы по
конкретному ключу и не разделяет read/write/scan. Для публичного или
масштабированного endpoint добавьте лимиты и, при необходимости, роли/scopes во
внешнем gateway; встроенный limiter остаётся лишь защитой от случайной локальной
нагрузки.

Не помещайте bearer token в URL, query string, `http_headers` в репозитории,
скриншот или chat prompt. Для клиента используйте secret store/environment.

Hash-based bearer — простой режим аутентификации self-hosted экземпляра. Если
выбранный публичный клиент требует стандартный MCP OAuth flow, разверните
проверенный OAuth/OIDC resource-server слой со scopes и audience validation либо
добавьте его в сервер; не отключайте auth ради совместимости. Требования к
стандартному flow сверяйте с
[MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).

Валидный встроенный bearer следует считать полностью привилегированным для всех
опубликованных MCP tools. Текст вакансии, переданный модели, не может сам вызвать
tool: клиентские approvals полезны, но server-side validation/policy остаётся
обязательной границей и не создаёт read-only роль.

## Семантика длительных операций

Scan tools не держат HTTP-соединение до конца работы:

1. `start_full_scan(source_id)` проверяет доступ и сразу возвращает `scan_id`;
2. worker выполняет scan с lock/checkpoint;
3. клиент вызывает `get_scan_status(scan_id)`;
4. batch tool возвращает `batch_id`, проверяемый отдельно.

Повторный client request с тем же idempotency context не должен создавать второй
одновременный full scan источника. Ошибка одного child scan не скрывает остальные.

`send_application` принимает только `application_id`. Recipient, MIME, body и
attachment не являются аргументами MCP и загружаются сервером после повторной
policy/idempotency проверки.

## Инструменты

### Профиль и предпочтения

- `get_system_status`
- `get_user_profile`
- `update_user_profile`
- `get_job_preferences`
- `update_job_preferences`
- `pause_auto_send`
- `resume_auto_send`

`resume_auto_send` — отдельное явное privileged действие. Обычный update профиля
или предпочтений не должен неявно включать автоотправку.

### Резюме

- `list_resumes`
- `upload_resume_metadata`
- `activate_resume`
- `deactivate_resume`

`upload_resume_metadata` не даёт LLM выбрать файл на сервере и не заменяет
защищённую загрузку PDF через панель/REST. Metadata связывается только с уже
безопасно сохранённым объектом либо создаёт ожидающую загрузку запись — в
зависимости от реализованного API. MCP не принимает filesystem path.

### Источники

- `list_sources`
- `get_source`
- `add_source`
- `update_source`
- `enable_source`
- `disable_source`
- `validate_source`
- `discover_categories`
- `get_source_health`

Source configuration проходит Pydantic/SSRF/domain validation. MCP не разрешает
произвольный fetch URL и не может выключить access-policy проверки.

### Scans

- `start_full_scan`
- `start_incremental_scan`
- `start_all_sources_incremental_scan`
- `get_scan_status`
- `get_batch_scan_status`

Каждая single-source команда принимает `source_id`. `start_all...` создаёт
`BatchScanRun`, а не подменяет source ID строкой `all`.

### Вакансии и matching

- `list_recent_jobs`
- `list_job_matches`
- `get_job`
- `analyze_job`

`analyze_job` запускает обычный deterministic + LLM pipeline. Клиент не передаёт
системный prompt, provider secret или итоговое policy decision.

### Applications

- `prepare_application`
- `approve_application`
- `decide_review`
- `send_application`
- `list_applications`
- `get_review_queue`
- `get_review_learning_status`
- `set_review_learning_influence`
- `get_application_status`

Approval не обходит hard safety rules. `send_application(application_id)` не
может отправить `blocked`, непроверенный или уже отправленный Application. Статус
`delivery_unknown` запрещает автоматический повтор.

`decide_review` — основной AI-first интерфейс для явного `approve`/`reject`.
Для отказа передаётся структурированный `reason_code`; `learn=false` исключает
конкретное решение из персональных подсказок. `get_review_queue` возвращает контекст
вакансии, причины policy review и, когда данных достаточно, объяснение похожих прошлых
решений. Обучение влияет только на сортировку и подсказки. Оно не меняет профиль,
подтверждённые факты, hard safety rules или состояние доставки.

### Отчёты

- `get_run_summary`
- `get_daily_report`

Ответы минимизируют PII. Recipient в отчёте доступен только роли, которой он
необходим; provider response предварительно санитизируется.

## MCP Inspector

Официальный Inspector можно запустить так:

```bash
npx @modelcontextprotocol/inspector
```

В UI выберите `Streamable HTTP`, укажите `https://job-agent.example/mcp` и добавьте
`Authorization: Bearer <token>` через защищённую настройку headers. Используйте
отдельный временный token для первоначального `tools/list`. Не публикуйте экспорт
Inspector config, если он содержит static header.

CLI-проверка списка tools поддерживается актуальным Inspector; перед использованием
сверьте его `--help`:

```bash
npx @modelcontextprotocol/inspector --cli \
  https://job-agent.example/mcp \
  --transport http \
  --method tools/list \
  --header "Authorization: Bearer ${JOB_AGENT_MCP_TOKEN}"
```

Требования Node и синтаксис могут меняться, поэтому источником истины остаётся
[официальный MCP Inspector](https://github.com/modelcontextprotocol/inspector).

Проверка считается успешной, если initialize и `tools/list` завершаются, схемы
аргументов присутствуют, запрос без/с неверным ключом отклоняется, а секреты не
появляются в ответе.

## Codex

Актуальные Codex CLI/IDE/desktop clients поддерживают remote Streamable HTTP и
bearer token из environment. Добавьте в `~/.codex/config.toml` (или проектный
`.codex/config.toml` только в доверенном репозитории):

```toml
[mcp_servers.job_agent]
url = "https://job-agent.example/mcp"
bearer_token_env_var = "JOB_AGENT_MCP_TOKEN"
default_tools_approval_mode = "writes"
tool_timeout_sec = 60
enabled = true
```

Задайте token вне файла:

```bash
export JOB_AGENT_MCP_TOKEN='<полученный-секрет>'
codex mcp list
```

Перезапустите соответствующий Codex client и откройте `/mcp`/список серверов.
Tool timeout не увеличивайте ради full scan: scan асинхронно возвращает ID.
Рекомендуется отдельно разрешить read tools и запрашивать client approval для
writes. Это настройка клиента, а не server-side role: скомпрометированный bearer
сохраняет доступ ко всем tools.

Формат проверен по [официальной документации Codex MCP](https://learn.chatgpt.com/docs/extend/mcp.md);
перед развёртыванием сверяйте её с установленной версией клиента.

## ChatGPT custom MCP app

Доступность добавления удалённого MCP app, расположение меню и требования к
аутентификации зависят от текущего продукта, тарифа и политики workspace. Если
ваш ChatGPT/workspace предоставляет создание custom MCP app:

1. создайте app/connector с transport `Streamable HTTP`;
2. задайте production URL `https://job-agent.example/mcp`;
3. настройте поддерживаемый клиентом способ bearer/OAuth-аутентификации;
4. начните с отдельного временного credential и не вызывайте write tools;
5. проверьте список tools и их input schemas;
6. разрешайте write tools на стороне клиента только после review администратора
   workspace; встроенный credential всё равно имеет полный набор прав;
7. протестируйте `get_system_status`, затем асинхронный fixture scan;
8. убедитесь, что `send_application` имеет только `application_id`.

Если текущий клиент не позволяет безопасно передать требуемую аутентификацию или
администратор запретил custom apps, не делайте endpoint публичным без auth и не
встраивайте token в URL. Используйте поддерживаемый клиент/identity gateway после
security review. Не утверждается, что эта функция доступна каждому аккаунту.

## Gemini-совместимый MCP-клиент

Подключение возможно только для клиента, который документированно поддерживает
MCP Streamable HTTP и передачу Authorization header. В его форме/конфигурации
укажите:

```json
{
  "name": "job-agent",
  "transport": "streamable-http",
  "url": "https://job-agent.example/mcp",
  "authorization": "Bearer token из secret store"
}
```

Это концептуальная схема, а не обещание конкретных имён полей. Используйте
актуальную документацию выбранного Gemini-совместимого клиента. Если он
поддерживает только stdio, может потребоваться локальный доверенный bridge, но
server authentication, TLS и write approvals сохраняются; не публикуйте
неаутентифицированный bridge.

## Обычный HTTP MCP-клиент

Клиент реализует именно MCP lifecycle, а не произвольный REST POST:

1. устанавливает TLS-соединение с `/mcp`;
2. отправляет MCP `initialize` и согласует protocol version/capabilities;
3. обрабатывает session ID, если сервер его выдаёт;
4. отправляет initialization notification;
5. вызывает `tools/list` и `tools/call`;
6. поддерживает JSON или SSE response согласно negotiated Streamable HTTP;
7. повторно передаёт Authorization и session headers;
8. корректно завершает/удаляет сессию, если сервер поддерживает это.

Reverse proxy должен пропускать GET/POST и необходимые MCP headers. Следуйте
[официальной спецификации Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
но используйте protocol version, согласованную сервером и клиентом, а не жёстко
зашитую из этого документа.

## Пример безопасного workflow

```text
list_sources
→ get_source_health(source_id)
→ start_incremental_scan(source_id)
→ get_scan_status(scan_id)
→ list_job_matches(filters)
→ prepare_application(canonical_job_id)
→ get_application_status(application_id)
```

Отправку вызывайте только для уже разрешённой Application:

```text
send_application(application_id)
```

Если policy вернула `pending_review`/`blocked`, MCP не меняет это произвольным
полем. `approve_application` — отдельное аудируемое действие, после которого hard
rules всё равно пересчитываются.

## Проверка аутентификации

Минимальный набор негативных тестов:

- без token → 401/соответствующая MCP auth error;
- неверный token → отказ без подсказки о существующем hash;
- token отсутствует или неверен при update/send → 401/403;
- hash удалён из конфигурации и API перезапущен → token получает немедленный отказ;
- malformed/oversized payload → controlled error;
- cross-origin browser request с недоверенного Origin → отказ;
- write call создаёт AuditEvent;
- response не содержит config secrets/encrypted token/resume path.

## Диагностика

- `401`: проверьте наличие hash в allowlist и HTTPS URL, не печатая token;
- `403`: проверьте `Origin` и policy-ограничение операции; встроенных
  bearer-ролей/scopes нет;
- timeout tool call: проверьте worker/Redis, длительная операция должна вернуть ID;
- tools отсутствуют: проверьте, что `/mcp`, а не REST root, и завершён initialize;
- session error за load balancer: проверьте передачу session headers и выбранное
  сервером session storage/stickiness;
- 502/504: проверьте Caddy/API readiness и streaming/buffering settings;
- schema error: обновите client/Inspector и сравните negotiated protocol version.

В логах ищите correlation ID, но не сохраняйте Authorization header.
