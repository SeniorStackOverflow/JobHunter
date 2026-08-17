# Архитектура job-agent

## Границы системы

`job-agent` — модульный монолит. API, MCP, административная панель, Celery worker и
Celery Beat используют один пакет доменной логики и одну PostgreSQL-схему. Redis нужен
для очереди, расписания, краткоживущих лимитов и распределённых блокировок, но не является
источником истины.

```text
source adapters -> scan pipeline -> normalized SourceJob -> CanonicalJob
                                                        |
profile + resumes -> deterministic filters -> LLM matcher -> MatchEvaluation
                                                        |
verified contact -> application generator -> policy engine -> email sender
                                                        |
                      REST / admin / MCP <- audit + reports + metrics
```

Celery Beat инициирует работу независимо от любого AI-клиента. MCP является только
аутентифицированным интерфейсом управления; отключение MCP не останавливает расписание.

## Слои

- `app/crawlers`: строгий `JobSourceAdapter`, реестр и общий scan pipeline. Адаптер не
  принимает решений о matching, письме или отправке.
- `app/deduplication`: сохраняет каждую публикацию как `SourceJob` и связывает её с
  обратимо объединяемым `CanonicalJob`.
- `app/profiles`: профиль, подтверждённые факты и безопасное хранилище PDF-резюме.
- `app/matching`: детерминированный prefilter и изолированный `LLMProvider` со строгой
  схемой ответа. Текст вакансии всегда передаётся как недоверенные данные.
- `app/contacts`: проверяет только опубликованные контакты и сохраняет evidence URL.
- `app/applications`: выбирает верифицированное резюме и строит письмо только из
  подтверждённых фактов.
- `app/policies`: полностью детерминированный fail-closed policy engine.
- `app/email`: sender принимает только `application_id`, повторно загружает все данные из
  БД и повторно проверяет policy/idempotency. LLM и MCP не формируют MIME.
- `app/scheduler`: задачи Celery и Redis locks; сбой источника не прерывает другие.
- `app/api`, `app/admin`, `app/mcp`: три транспорта над одними сервисами.
- `app/audit`, `app/observability`: структурированные события, метрики и диагностика без
  секретов и полного текста резюме.

## Источники

`JobSource.adapter_type` выбирается через `JobSourceAdapterRegistry`. Реестр поддерживает
`rabota_md`, `generic_html`, `generic_api`, `rss`, `sitemap`, `company_careers` и
`fixture_source`. Не реализованные для конкретной конфигурации типы регистрируются как
явно неподдерживаемые и не возвращают фиктивный успех.

Full scan объединяет общую выдачу, категории, подкатегории, регионы и только разрешённые
публичные feed/sitemap-точки. Reference дедуплицируется по внешнему ID до загрузки detail
page. Checkpoint сохраняется после каждой успешно обработанной ссылки. Incremental scan
использует те же контракты, но ограничивает глубину и останавливается после заданного числа
известных неизменённых записей.

## Данные и идемпотентность

- `UNIQUE(source_id, external_job_id)` защищает от дубля публикации.
- Локализованные URL сохраняются внутри одной `SourceJob`.
- `CanonicalJob` объединяет публикации, но исходные записи и снимки не удаляются.
- Одна `Application` на `canonical_job_id` по умолчанию исключает повторный отклик.
- `Application.idempotency_key` и уникальная `EmailDelivery.application_id` защищают от
  повторного Celery task и двойного клика.
- `delivery_unknown` — терминальное для автоматического retry состояние: требуется ручная
  сверка с Gmail.

## Статусы закрытия

Сетевая ошибка не считается отсутствием. Подтверждённое отсутствие переводит вакансию из
`active` в `possibly_closed`; только заданное число последовательных подтверждений или
явная страница закрытия переводит в `closed`. При деградации адаптера массовые переходы
отключаются.

## Доверительные границы

HTML, JSON-LD, URL, email, текст вакансии и результат LLM недоверенны. До HTTP-запроса URL
проходит allowlist домена, DNS/IP-проверку, запрет private/link-local/loopback/metadata
адресов и проверку каждого redirect. Получатель, вложение, лимит и флаг автоотправки
никогда не берутся из текста вакансии или LLM-ответа.

Реальная доставка имеет два независимых выключателя:

1. deployment-флаг `REAL_EMAIL_DELIVERY_ENABLED=false` по умолчанию;
2. пользовательские `auto_send_enabled`, `global_pause`, категории и дневной лимит.

Аварийный `EMERGENCY_EMAIL_KILL_SWITCH` имеет приоритет над обоими.

