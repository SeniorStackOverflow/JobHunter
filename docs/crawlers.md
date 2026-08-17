# Краулеры и обнаружение вакансий

Crawler subsystem получает только публично доступные данные с разрешённой
глубиной, нормализует их и передаёт в общий pipeline. Он не оценивает кандидата,
не выбирает резюме и не отправляет отклики.

## Слои

```text
JobSource + configuration
        ↓
adapter registry
        ↓
access-policy check / discovery
        ↓
full | incremental | recheck
        ↓
raw reference → raw details → normalized job
        ↓
SourceJob upsert + snapshot + scan statistics
        ↓
canonical deduplication → matching pipeline
```

`RabotaMdAdapter`, `GenericHtmlSourceAdapter` и `FixtureSourceAdapter` реализуют
один контракт. Site-specific разметка не выходит за пределы adapter/parser.

## Access policy до обхода

Перед первым scan и периодически далее adapter проверяет:

- допустимость `base_url` и allowlist доменов;
- собственные умеренные rate limits;
- зафиксированные публичные условия;
- отсутствие login/CAPTCHA/anti-bot обхода;
- разрешённые API/feed/sitemap/HTML entrypoints;
- rate/concurrency и redirect policy.

Изменение access policy является health-событием. При запрете или неоднозначности
источник безопасно приостанавливается; это не повод искать альтернативный скрытый
endpoint.

## Discovery

Discovery выполняется динамически и сохраняет `SourceCategory` с parent, locale,
URL, active и last-seen. Источник может дополнительно вернуть locales и regions.
URL проходят canonicalization и SSRF-validation до запроса.

Не следует считать заранее известный path постоянным контрактом. Start URL может
быть сконфигурирован, но ссылки категорий, pagination и вакансий извлекаются из
актуальной разрешённой страницы/structured data.

Исчезнувшая категория не удаляется немедленно: она помечается неактивной только
после завершённого надёжного discovery, чтобы сохранить историю и избежать реакции
на временную поломку parser-а.

## Full scan

Full scan пытается охватить все активные объявления, которые источник в данный
момент публично отдаёт. Он не обещает удалённый или непубличный архив.

Entry points объединяются:

- общая выдача;
- категории и подкатегории;
- региональные/городские выдачи;
- локализованные выдачи;
- разрешённые sitemap/feed/API;
- дополнительные публичные точки, подтверждённые исследованием.

Очередь entrypoints и pagination обрабатывается до доступного конца/явного
ограничения. References объединяются по source identity/canonical/localized URL до
дорогого details fetch. Все категории/регионы, где встретилась публикация,
сохраняются.

`ScanRun` отражает реальную глубину: discovered categories, entrypoints, pages,
found/new/updated/unchanged, parsing/network errors и checkpoint. Ограничение
глубины сайта или конфигурации явно попадает в summary.

## Checkpoint и возобновление

Checkpoint хранится в PostgreSQL в `ScanRun`, является JSON-сериализуемым и
версионированным. Типичное содержимое:

- версия adapter/config;
- scan mode;
- pending/finished entrypoints;
- текущий page/cursor;
- компактный набор уже обработанных reference IDs;
- counters и timestamp;
- cursor-specific stop state.

Обновление checkpoint и сохранение обработанной порции выполняется согласованно.
Upsert по `(source_id, external_job_id)`/устойчивой identity делает повтор порции
безопасным. Нельзя хранить единственную копию checkpoint только в Redis.

После несовместимого изменения adapter checkpoint не интерпретируется молча:
run помечается требующим controlled restart, старый checkpoint остаётся в
диагностике.

## Incremental scan

Инкрементальный режим оптимизирован для запуска каждые несколько часов:

- заново выполняет лёгкий discovery категорий;
- читает свежие страницы общей выдачи;
- читает первые configured pages каждой категории/региона;
- сравнивает external ID, content hash, `published_at` и `updated_at`;
- обнаруживает поднятые вакансии, даже если publication date старая;
- прекращает entrypoint после configured последовательности известных
  неизменённых записей;
- продолжает другие entrypoints независимо.

Порог неизменённых записей не применяется глобально: иначе большой известный
раздел может скрыть новую вакансию в другом регионе/категории.

## Fetch, parsing и normalization

Fetch layer обеспечивает:

- один идентифицируемый user agent без маскировки человека;
- per-source token bucket/rate limiter;
- ограниченную concurrency;
- timeouts и ограниченные retries только для безопасных GET;
- backoff/jitter для 429/5xx;
- максимальный размер ответа;
- allowlist content types;
- проверку каждого redirect;
- метрики без сохранения лишнего body.

HTML parser предпочитается browser automation. Playwright разрешён только для
конкретного adapter/config, с теми же URL/egress ограничениями, без login и обхода
защит.

Нормализация сохраняет `None`, если поле отсутствует. Даты приводятся к UTC с
сохранением исходного значения/locale metadata, salary — к min/max/currency без
догадки, HTML — к безопасному тексту. Ответ vacancy page не может изменять
конфигурацию или дать команду системе.

## Сохранение и snapshots

SourceJob — конкретная публикация конкретного источника. Upsert обновляет
`last_seen_at` всегда, но JobSnapshot создаётся только при существенном изменении
нормализованных полей/content hash. `first_seen_at` не перезаписывается.

Отдельно сохраняются:

- `published_at` источника;
- `updated_at` источника;
- `first_seen_at`/`last_seen_at` сервиса;
- `last_checked_at` recheck;
- localized URLs и категории появления;
- raw metadata в минимальном безопасном объёме.

## Deduplication

Внутрисайтовая identity сначала использует `(source, external ID)`, canonical и
localized URL. Затем общий dedup связывает SourceJob с CanonicalJob по набору
сигналов: normalized company/title/location, employer domain/contact, salary,
dates, content hash и semantic similarity.

Слабое сходство не приводит к необратимому merge. Исходные SourceJob сохраняются,
relation можно разъединить. Уникальность отклика контролируется на CanonicalJob,
а не только на URL публикации.

## Recheck активных вакансий

Периодическая выборка active jobs проверяет существование и изменения. Переход:

```text
active → possibly_closed → closed
```

`closed` устанавливается при явном корректно распарсенном сообщении о закрытии
или после configured числа независимых подтверждённых отсутствий. Timeout,
DNS failure, 429, 5xx, CAPTCHA, login redirect и parser anomaly не увеличивают
счётчик подтверждённого отсутствия.

Если одновременно исчезает большая доля страниц или обязательных элементов,
срабатывает circuit breaker источника: recheck transitions и downstream
applications приостанавливаются.

## Мониторинг деградации

Health evaluator сравнивает scan с baseline и абсолютными guards:

- нулевой или аномально низкий результат;
- parsing error ratio;
- обязательные поля/ID отсутствуют;
- неожиданные host/path/redirect;
- 403/429/CAPTCHA/login;
- access-policy change;
- content/URL structure drift.

Решение degraded должно сопровождаться alert и безопасной диагностикой. При этом:

- источник не создаёт applications из неполных записей;
- массовое закрытие отключено;
- остальные источники продолжают работу;
- исторические записи не удаляются.

## SSRF и URL lifecycle

URL валидируется при конфигурации, discovery и непосредственно перед каждым
сетевым соединением. Запрещаются:

- localhost и `127.0.0.0/8`;
- RFC1918 private IPv4;
- link-local, unspecified, multicast и reserved ranges;
- IPv6 loopback, ULA и link-local;
- cloud metadata endpoints;
- URL с embedded credentials;
- host вне source allowlist;
- redirect на запрещённый host/IP.

DNS-resolve проверяется для всех адресов; соединение должно быть привязано к
проверенному результату или повторно проверять IP, чтобы снизить DNS-rebinding.
Сетевой egress firewall дублирует запреты приложения.

URL из описания вакансии не становится автоматически crawl target. Employer URL,
contact evidence и application URL проходят отдельную allowlist/official-domain
проверку.

## Планирование и locks

У каждого источника собственные full/incremental/recheck schedules. Celery Beat
публикует задания, worker получает distributed lock по `(operation, source_id)`.
Lock имеет ограниченный TTL и owner token; освобождать чужой lock нельзя.

После scan processing запускается по сохранённым IDs/range, а не по недоверенным
командам source. Ошибка processing не должна заставлять повторно скачать весь сайт.

## Fixture source

Fixture adapter и локальный fixture site являются полноценным независимым
источником для CI/E2E, а не условием в production pipeline. Он моделирует другую
HTML-структуру, pagination, локали, старые/новые/updated/raised/closed jobs,
межсайтовый дубль, scam и prompt injection. Fake provider разрешён в тестовом
окружении; production конфигурация не должна случайно считать fixture контакты
реальными.

## Диагностический checklist scan

- access-policy status записан;
- source/config/adapter version известны;
- rate limit и concurrency соблюдены;
- все entrypoints и stop reasons посчитаны;
- checkpoint обновляется;
- `first_seen_at` не изменился у старых jobs;
- snapshots создаются только для значимых изменений;
- localized pages объединены;
- network error не стал closed;
- source degradation блокирует downstream;
- логи не содержат PII/полный HTML;
- report агрегаты совпадают с ScanRun/BatchScanRun.
