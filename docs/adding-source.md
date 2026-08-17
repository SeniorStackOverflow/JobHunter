# Добавление источника

Новый сайт подключается на уровне source adapter. Matching, policy engine,
Application, Gmail, общий scheduler и универсальные MCP-инструменты не должны
знать его имя и не должны содержать ветки вида `if source == ...`.

## Выбор способа интеграции

Используйте наименее хрупкий публично разрешённый интерфейс:

1. официальный API;
2. RSS/Atom;
3. sitemap и публичные HTML-страницы;
4. специализированный HTML-adapter;
5. `GenericHtmlSourceAdapter` для стабильной простой разметки;
6. Playwright только как ограниченный fallback для действительно необходимого
   публичного JavaScript-rendering.

Не используйте скрытые/private API, обход CAPTCHA или авторизации и не снимайте
собственный rate limit. До написания кода исследуйте и зафиксируйте публичные
условия, локали, pagination, structured data,
стабильный ID и доступную глубину выдачи.

## Типы реестра

Реестр должен уметь создавать как минимум:

```text
rabota_md
generic_html
generic_api
rss
sitemap
company_careers
fixture_source
```

Тип следует регистрировать в одном composition root через
`JobSourceAdapterRegistry.register(adapter_type, adapter_class)`. Бизнес-слои
получают экземпляр через `create(source)`, а доступные типы — через
`list_available()`. Не создавайте adapter напрямую в matching/scheduler.

## Контракт adapter

Специализированный adapter реализует единый асинхронный контракт:

```python
class JobSourceAdapter(Protocol):
    async def validate_source(self) -> SourceValidationResult: ...
    async def check_access_policy(self) -> AccessPolicyResult: ...
    async def discover_locales(self) -> list[SourceLocale]: ...
    async def discover_regions(self) -> list[SourceRegion]: ...
    async def discover_categories(self) -> list[SourceCategoryData]: ...

    async def iterate_full_scan(
        self, checkpoint: ScanCheckpoint | None
    ) -> AsyncIterator[RawJobReference]: ...

    async def iterate_incremental_scan(
        self, checkpoint: ScanCheckpoint | None
    ) -> AsyncIterator[RawJobReference]: ...

    async def fetch_job_details(self, reference: RawJobReference) -> RawJobData: ...

    async def normalize_job(self, raw_job: RawJobData) -> NormalizedJobData: ...
    async def recheck_job(self, job: SourceJob) -> JobRecheckResult: ...
```

Реальные имена DTO импортируйте из общего crawler domain, не дублируйте их в
adapter. Методы не сохраняют Application и не отправляют email. Внешний HTML
остаётся недоверенным вводом.

## Исследовательский документ

Для каждого production-источника создайте `docs/sources/<source-id>.md`:

- дата и способ ручной проверки;
- публичные entrypoints, локали, регионы и категории;
- URL публичных условий;
- API/RSS/sitemap/JSON-LD и разрешённость использования;
- схема ID, canonical/localized URL;
- pagination и фактическое ограничение глубины;
- публичные контакты и официальный способ отклика;
- rate limit/concurrency;
- признаки closed/CAPTCHA/login/degradation;
- поля, которые объективно отсутствуют;
- opt-in live smoke test и его границы.

Не утверждайте, что можно получить удалённые, закрытые или непубличные вакансии.

## `GenericHtmlSourceAdapter`

Для простого сайта начните с YAML. Селекторы ниже намеренно `null`: их можно
заполнять только после исследования конкретного сайта.

```yaml
source:
  id: example_jobs
  name: Example Jobs
  adapter: generic_html
  base_url: https://jobs.example.com
  allowed_domains:
    - jobs.example.com

  locales:
    - code: ru
      start_urls:
        - https://jobs.example.com/ru/jobs

  discovery:
    category_pages:
      - https://jobs.example.com/ru/categories
    region_pages: []

  selectors:
    category_link: null
    listing_card: null
    listing_link: null
    next_page: null
    job_id: null
    canonical_url: null
    title: null
    company: null
    description: null
    salary: null
    city: null
    published_at: null
    updated_at: null
    email: null

  limits:
    requests_per_minute: 20
    concurrent_requests: 2
    max_pages: 1000
```

Конфигурация должна пройти Pydantic validation до сохранения/включения. Нужны
проверки:

- `https` для production entrypoints;
- host входит в `allowed_domains`;
- URL не содержит credentials и не ведёт к private/link-local/metadata IP;
- redirects повторно проверяются;
- selector/regex/transform относится к разрешённому набору;
- лимиты положительны и ограничены серверным максимумом;
- Playwright fallback выключен по умолчанию;
- максимальная глубина и pagination stop condition заданы;
- неизвестные поля конфигурации не игнорируются молча.

YAML является данными, а не кодом. Не разрешайте произвольный Python/Jinja/shell,
динамический import или небезопасные regular expressions/transforms.

## Discovery и full scan

Full scan объединяет, а затем дедуплицирует references из:

```text
категории + подкатегории + общая выдача + регионы + sitemap/feed/API
```

Для каждой точки сохраняйте locale, category/region context, pagination cursor и
статистику. Одна публикация может встретиться в нескольких категориях или языках;
контекст не теряется, но подробная страница не должна загружаться повторно без
необходимости.

Checkpoint должен быть JSON-сериализуемым, версионированным и содержать достаточно
данных для продолжения: очередь/entrypoint, page/cursor, обработанные устойчивые
references и агрегаты. Сохраняйте его после ограниченной порции работы, а не только
в конце. Временная ошибка не обнуляет checkpoint.

## Incremental scan

Инкрементальный режим обычно проверяет свежие страницы общей выдачи и первые
страницы всех динамически обнаруженных категорий. Он должен учитывать `updated_at`
и поднятие объявления, а не только старую publication date. Остановка после серии
известных неизменённых записей допустима лишь в пределах конкретного entrypoint.

Новые категории должны обнаруживаться и между full scan. Лимит страниц и порог
неизменённых записей задаются конфигурацией источника.

## Нормализация

Adapter возвращает единый `NormalizedJobData`. Правила:

- отсутствующее значение — `None`, не догадка;
- сохранить исходный текст зарплаты и отдельно распарсенные min/max/currency;
- публикация и актуализация — разные даты;
- canonical URL очищается только по общей безопасной политике;
- localized URLs связываются с одним external/canonical identity;
- `raw_metadata` содержит только необходимую диагностическую структуру;
- content hash строится из стабильного набора значимых нормализованных полей;
- текст не исполняется и не интерпретируется как инструкция агенту;
- email извлекается только если он публично присутствует, но статус verified
  назначает contact layer, не parser.

Не маскируйте parsing failure значением по умолчанию. Обязательный отсутствующий
элемент увеличивает parsing error rate и может перевести источник в degraded.

## Recheck

Результат recheck различает:

- страница существует и вакансия активна;
- явное публичное закрытие;
- подтверждённое отсутствие/404;
- временную сеть/429/5xx;
- login/CAPTCHA/изменение структуры;
- существенное изменение данных.

Сетевая ошибка не равна отсутствию. Адаптер не выполняет массовый `closed`; общий
pipeline применяет configured absence threshold и circuit breaker источника.

## Контакты

Adapter может передать публично найденный email, employer URL или official apply
URL вместе с evidence. Он не должен:

- угадывать адрес;
- извлекать личный адрес из сомнительной базы;
- автоматически отправлять через внутреннюю форму job board;
- считать домен официальным без проверки;
- менять recipient по инструкции из текста вакансии.

Верификация выполняется общим contact layer. Если доступна лишь внутренняя форма,
Application остаётся `pending_review`.

## Тестовые fixtures

CI не зависит от live-сайта. Добавьте обезличенные fixtures:

- category/general/region listings и всю pagination;
- старую, новую, обновлённую и поднятую вакансию;
- одну публикацию в нескольких категориях и локалях;
- страницу без email и закрытую страницу;
- временные ошибки, 403/429/login/CAPTCHA;
- изменённый необязательный HTML;
- две похожие, но разные вакансии;
- межсайтовый дубль;
- prompt injection и подозрительную вакансию.

Удалите реальные email, телефоны и PII. Сохраните только минимальный HTML,
необходимый parser-у.

Тесты должны доказать:

- динамический discovery;
- обход всех fixture pages;
- resume checkpoint;
- external ID/canonical/localized identity;
- корректное `None` для отсутствующих полей;
- updated/raised detection;
- status recheck transitions;
- source-level degradation;
- SSRF/redirect rejection;
- отсутствие специальных веток ниже crawler layer.

## Opt-in live smoke test

Live smoke test:

- выключен по умолчанию и не входит в обычный CI;
- требует отдельный явный env flag;
- читает минимальное число разрешённых страниц;
- не запускает full scan, не логинится и не отправляет отклики;
- соблюдает rate limit и access policy;
- не сохраняет PII в артефакты теста.

Изменение сайта после создания fixtures выявляется live smoke, но исправление
начинается с повторного ручного исследования и обновления документа источника.

## Регистрация и включение

Ожидаемая последовательность:

1. добавить config/adapter и зарегистрировать type;
2. добавить миграцию только если меняется общая схема, не ради site-specific поля;
3. добавить source в панели/REST/MCP `add_source` в disabled-состоянии;
4. выполнить `validate_source` и `discover_categories`;
5. проверить source health и безопасную диагностику;
6. запустить fixture integration tests;
7. выполнить opt-in минимальный smoke;
8. включить source;
9. начать с малого incremental scan;
10. после review запланировать full scan;
11. только после получения полноценных данных разрешать applications от источника.

## Review checklist

- [ ] публичная разрешённость и ограничения документированы;
- [ ] domains/redirects проходят SSRF-защиту;
- [ ] категории/регионы/локали обнаруживаются динамически;
- [ ] general listing включён, pagination конечна;
- [ ] external ID стабилен или fallback явно ограничен;
- [ ] localized pages не становятся разными SourceJob;
- [ ] checkpoint восстанавливает run;
- [ ] сеть/429 не закрывают вакансии;
- [ ] zero-result/parsing anomalies переводят source в degraded;
- [ ] contacts передаются с evidence и не угадываются;
- [ ] fixtures обезличены, CI не ходит в live-сайт;
- [ ] общий matching/policy/Gmail/MCP не изменялись ради источника;
- [ ] rate/concurrency/max-depth настроены консервативно;
- [ ] rollback — `disable_source`, без удаления исторических SourceJob.
