# Rabota.md

Дата актуализации реализации: 2026-08-11.

## Рабочая поверхность

Crawler читает публичные страницы `https://www.rabota.md/ru/vacancies` и
верхнеуровневые категории вида `/ru/vacancies/category/<slug>`. Ссылки на
вложенные профессии намеренно не становятся отдельными entrypoint.

Для Rabota.md используется один persistent Chromium context с
`playwright-stealth`. Он сохраняет cookies между listing/detail запросами и
закрывается в конце scan. Встроенный HTTP adapter остаётся только тестовым и
аварийным transport.

## Пагинация

Первая страница категории открывается обычной browser navigation. Следующие
страницы берутся из фактического `data-next` текущего HTML и загружаются в том же
browser context через `POST` с `X-Requested-With: XMLHttpRequest`. Ответ обязан
иметь форму `success=true` и строковый `data.content`.

Ограничения:

- максимум 100 страниц на категорию для full scan;
- максимум 20 страниц на incremental scan;
- обнаруженные page URL и vacancy ID защищены от циклов;
- нулевой результат, резкое падение количества и высокая доля parse errors
  переводят источник в degraded/partial;
- CAPTCHA, login redirect, 403 и 429 не считаются пустой выдачей.

## Умеренная интенсивность

Общий limiter экземпляра пропускает не более 50 browser-запросов в минуту с
интервалом не менее 1,2 секунды. Контекст и page общие, поэтому listing, AJAX и
detail requests не могут обойти этот предел параллельными ветками. Изображения,
media, fonts и websocket блокируются как ненужные для извлечения вакансий.

Расписание production-конфигурации:

- hourly incremental: только верхнеуровневая категория `others`;
- daily full: все верхнеуровневые категории;
- daily active-job recheck: отдельная операция.

Hourly incremental предназначен прежде всего для новых external ID. Если listing
не отдаёт update timestamp, detail уже известной вакансии повторно загружается не
чаще одного раза за 24 часа; новый ID всегда загружается полностью. Daily full не
использует этот shortcut и остаётся полным refresh всех категорий. На реальной
странице 100 известных вакансий такой incremental занимает около 9 секунд вместо
примерно 4 минут.

Источник после seed остаётся выключенным и с paused downstream. Включение
выполняется оператором после малого live smoke и проверки публичных условий.
Crawling и расписания после включения работают независимо от downstream-паузы;
matching, applications и email из paused scan автоматически не ставятся в очередь.

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

Телефоны нормализуются в E.164 через libphonenumber. Поддерживаются молдавские
мобильные и стационарные номера, `+<country code>` и международный префикс `00`,
но сохраняются только номера с валидным планом нумерации. `tel:` извлекаются только
из vacancy-owned блоков: глобальные support/header телефоны Rabota.md не становятся
контактами работодателя. Все найденные значения сохраняются в
`public_emails`/`public_phones`; первый валидный контакт дублируется в
`public_email`/`public_phone` для совместимости с application pipeline.

## Проверки

- unit fixtures проверяют верхнеуровневые категории, AJAX pagination, checkpoint,
  full description, email и расширенную матрицу телефонов;
- `scripts/check_playwright_stealth.py` выполняет opt-in smoke на Sannysoft и
  сохраняет JSON-диагностику без cookies;
- live Rabota smoke получает ограниченное число реальных вакансий и не отправляет
  отклики;
- полный application/email test выполняется отдельно через policy engine и
  idempotency key с заранее выбранными vacancy, recipient, resume и текстом.
