# Rabota.md: текущий источник и транспорт

Актуализировано 2026-09-30 по `app/crawlers/adapters/rabota_md`,
`config/sources/rabota-md.yaml` и production Compose. Это описание реализации,
а не результат нового live crawl или проверки публичных условий сайта.

## Рабочая поверхность

Источник `rabota_md` читает публичную `/ru/vacancies` и верхнеуровневые категории
`/ru/vacancies/category/<slug>`. Вложенные профессии не создают отдельный entrypoint.
Основной PROD transport — `waf_http`: HTTP fetcher, bounded pure-Python AWS WAF
solver и egress validation. Стандартный image без Chromium, runtime fallback —
`none`. Старое утверждение «persistent Chromium — основной транспорт» не актуально.

Production использует primary egress и proven proxy reserve. Успешный сетевой
ответ сам по себе не делает proxy ready: необходимы landing + pagination proof.
Maintenance обслуживает отдельный `proxy-worker`; параметры и bounded failure
states описаны в [emergency/reserve runbook](../operations/rabota-browser-emergency.md).

Browser transport сохранён как отдельно разрешаемый emergency profile. Его
активация меняет crawler image и является исключением из обычного single-image
rollout; это не автоматический fallback основного PROD. Не устанавливайте Chromium
в основной image и не запускайте live smoke без разрешения ради проверки docs.

## Пагинация и ограничения

Следующая страница извлекается из фактического `data-next` и запрашивается POST
с `X-Requested-With: XMLHttpRequest`. JSON обязан содержать `success=true` и
строковый `data.content`. Landing/detail/pagination используют согласованный
transport/egress; WAF cookies и токены не публикуются в диагностике.

По умолчанию full scan ограничен 100 страницами на entrypoint, incremental — 20.
Защита от повторных URL/ID предотвращает циклы. Rate — 50 запросов в минуту с
minimum interval 1,2 секунды. Challenge, CAPTCHA, login redirect, 403/429,
network/parse failures не превращаются в «пустую выдачу» и не подтверждают
закрытие вакансии. При деградации массовые close transitions блокируются.

## Расписание и конфигурация

Конфигурация, сохранённая в `JobSource.configuration`, управляет реальным
расписанием. YAML и `job-agent seed` создают разные исходные настройки; seed не
обновляет уже существующий источник и не является production migration.

| Настройка | `config/sources/rabota-md.yaml` | Новый seed из CLI |
| --- | --- | --- |
| Transport | `waf_http`, fallback `none` | legacy `use_stealth_browser=true` |
| Incremental | `0 * * * *`, категория `others` | То же |
| Active recheck | `0 2 * * *` | `20 * * * *`, max 300, min interval 20 h |
| Full | `0 3 * * *`, checkpoint resume | То же |
| Enabled / downstream | disabled / paused после безопасного seed | disabled / paused |

Перед включением нового источника проверьте actual DB configuration и задайте
production transport явно. Не предполагается, что YAML автоматически загружен
или seed уже соответствует browser-free PROD. При явном `transport` убирайте
противоречащий legacy `use_stealth_browser`.

Incremental всегда загружает новые ID. Известные detail refresh по умолчанию
откладываются на 72 часа с deterministic jitter до 12 часов; budget — 50 refresh
на run. Daily full остаётся refresh доступной выдачи. Время/глубина зависят от
фактического сайта; старое число секунд на конкретной live странице не является SLA.

Физический crawl общий; per-profile выбор источников ограничивает downstream.
При `automatic_actions_paused` crawl может продолжаться, но matching/applications
не запускаются из paused scan. Account/profile readiness gates действуют отдельно.

## Нормализация

Устойчивый внешний ID берётся из числового ID detail URL. Один SourceJob может
встречаться в нескольких категориях, но detail page загружается один раз, а
`categories_seen` объединяются.

Matching хранит источник-категории без подмены вложенными профессиями. Узкая
детерминированная карта переводит только официальные slug `warehouses` в
пользовательскую категорию `warehouse` и `transport` в `logistics`; свободный текст
описания не может самостоятельно расширить allowlist.

С detail page извлекаются:

- title, company и employer URL;
- полное описание `.vacancy-content` с fallback на JSON-LD;
- требования и обязанности;
- зарплата, валюта, города, график, опыт и workplace type;
- даты публикации/обновления и признак закрытия;
- все публичные email и телефоны;
- внешний application URL либо наличие публичного внутреннего отклика.

`salary_text` сохраняет исходное отображаемое значение, включая `Не указана`, для
аудита и интерфейса. Числовые `salary_min`/`salary_max` остаются отдельными полями;
обучение не определяет наличие зарплаты по одной лишь непустой строке. Структурированное
поле опыта имеет приоритет над случайными фразами в полном описании: явное `С опытом`
не может превратиться в `no_experience=true` из-за соседнего упоминания вакансий без
опыта.

Телефоны нормализуются в E.164 через libphonenumber. Поддерживаются молдавские
мобильные и стационарные номера, `+<country code>` и международный префикс `00`,
но сохраняются только номера с валидным планом нумерации. `tel:` извлекаются только
из vacancy-owned блоков: глобальные support/header телефоны Rabota.md не становятся
контактами работодателя. Все найденные значения сохраняются в
`public_emails`/`public_phones`; первый валидный контакт дублируется в
`public_email`/`public_phone` для совместимости с application pipeline.

## Проверки и история исследования

Локальные fixtures проверяют listing/detail normalization, пагинацию, checkpoint,
WAF failure taxonomy, proxy proof и bounded retry. Live Rabota smoke отдельно
opt-in, с ограниченной глубиной и без отправки откликов. Полный application/email
E2E выполняется на fixture source и fake Gmail.

[HTTP/WAF design](rabota-md-http.md) и [pure-Python spike](rabota-md-pure-python.md)
сохранены как исторические исследования. Их прежние fallback proposals и фраза
«реализация не начата» не описывают текущий production.
