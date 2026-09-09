# Дизайн: консистентность профиля и резюме в веб-панели

- **Дата:** 2026-09-09
- **Статус:** проект, ожидает ревью оператора
- **Ветка:** `feature/profile-resume-panel`
- **Связанные документы:** [`docs/architecture.md`](../../architecture.md),
  [`docs/security.md`](../../security.md),
  [`docs/threat-model.md`](../../threat-model.md),
  [`README.md`](../../../README.md)

---

## 1. Контекст и проблема

Раздел `Настройки → Профиль кандидата / Резюме` в админ-панели (`app/admin/`)
накопил несогласованности:

| Симптом | Текущее состояние в коде |
|---|---|
| Поля профиля не используются | `UserProfile.phone` и `contact_email` не читает ни `generate_letter`, ни телефонный агент, ни policy engine. Оба поля попадают только в `profile_fingerprint`, то есть их правка **зря инвалидирует** уже посчитанные `MatchEvaluation`. |
| Письмо работодателю без обратного контакта | `generate_letter` (`app/applications/service.py:89`) собирает подпись как `С уважением,\n{profile.name}` — HR некуда перезвонить. |
| Резюме нельзя убрать из панели | Панель умеет только загрузить (`POST /admin/resumes`) и подтвердить (`POST /admin/resumes/{id}/verify`). Деактивация есть только в MCP (`deactivate_resume`); удаления нет нигде. |
| Резюме нельзя открыть | Ни маршрута скачивания/просмотра. `read_verified_resume` вызывается только при вложении в письмо. |
| Нельзя создать профиль сразу с резюме | `POST /admin/profiles` принимает только `name`. Загрузка резюме требует уже существующий выбранный профиль — два шага, две вложенные формы. |
| Формы плодятся | «Создать профиль» — `<details>` внутри `<details>` секции «Профиль кандидата»; «Загрузить резюме» — ещё один вложенный `<details>` секции «Резюме». Профиль и его резюме визуально разорваны. |

Страница настроек **уже** привязана к профилю: переключатель профиля в топбаре
(`dashboard.html:28`, `<select data-profile-select>` c GET по `profile_id`),
`selected_profile_id` в контексте, форма «Критерии и лимиты» уже per-profile.
Не хватает согласованной подачи и недостающих действий.

## 2. Цель и не-цели

### 2.1. Цель

1. Оживить `phone` и `contact_email`: включить их в подпись отправляемого письма.
2. Дать в панели полный жизненный цикл резюме: **просмотр PDF**,
   **деактивация/реактивация**, **условное удаление**.
3. Разрешить создание профиля вместе с первым резюме одной формой.
4. Перестроить раздел в один блок «Профиль» с единым паттерном списков и без
   вложенных `<details>`.
5. Добавить паритет REST/MCP для удаления и активации резюме.

### 2.2. Не-цели

- Не парсим, не редактируем и не генерируем PDF-резюме. Резюме остаётся
  загружаемым файлом-вложением.
- Не форсируем перегенерацию писем уже подготовленных заявок — новые заявки
  получают подпись, in-flight обрабатываются существующими правилами re-prepare.
- Не меняем схему БД (миграции нет). Удаление — жёсткое и условное, без
  `archived`/`deleted_at`.
- Не добавляем в панель редакторы `work_experience`, `education`,
  `confirmed_facts`, `availability` — они остаются на REST `PUT /api/v1/profile`
  и MCP `update_user_profile`.
- Не принуждаем `profile.phone` к формату E.164 — подпись читает человек.
- Не трогаем пути `auto_send_enabled` / `global_pause` (только явные pause/resume).
- Не чиним потерю уровня языка (`level`) при вводе языков через панель — см. §10.

## 3. Ключевые решения

| Вопрос | Решение |
|---|---|
| Куда попадают имя/телефон/почта | Только в профиль; из профиля — в подпись письма (`generate_letter`). PDF остаётся независимым вложением. |
| Просмотр резюме оператором | Разрешён: `GET /admin/resumes/{id}/file`, `application/pdf` inline, за `require_admin_page`. Не противоречит threat-model — ограничение «никаких путей/содержимого» относится к MCP (низкодоверенный токен), не к аутентифицированной панели. |
| Удаление резюме | Только при отсутствии ссылок из `Application` и `MatchEvaluation` (обе FK — `ondelete="RESTRICT"`). Иначе — деактивация. |
| Мягкое удаление | Отклонено: скрытое состояние хуже явного; история отклика с `resume_id` должна оставаться целой; жёсткое удаление достаточно для «залил не тот файл». |
| Структура панели | Единый блок «Профиль» на выбранный профиль: поля + список резюме (строки с inline-действиями + одна видимая строка добавления) + ссылка на критерии. «Новый профиль» — один блочный `<details>` с полем первого резюме. |
| Миграция схемы | Не требуется. |

## 4. Изменения по слоям

### 4.1. `app/applications/service.py` — подпись письма

`generate_letter(profile, job) -> tuple[str, str, str, list[str]]` — единственная
точка сборки письма. В блок подписи каждого языка добавляются строки контактов,
**только когда поле непусто** (после `strip()`):

| Язык | Базовая подпись | Доп. строки (если заполнено) |
|---|---|---|
| ru | `С уважением,\n{name}` | `\nТел.: {phone}` затем `\nEmail: {contact_email}` |
| ro | `Cu respect,\n{name}` | `\nTel.: {phone}` затем `\nEmail: {contact_email}` |
| en | `Kind regards,\n{name}` | `\nPhone: {phone}` затем `\nEmail: {contact_email}` |

Свойства:
- При обоих пустых полях тело письма **байт-в-байт** совпадает с текущим.
- `phone` и `contact_email` — не «claims»: `all_claims_confirmed`,
  `used_confirmed_facts` и `confirmed_facts` не затрагиваются.
- `content_validated` (`job.title in subject`, `job.company in body`,
  `detect_prompt_injection(body)`) от новых строк не ломается.
- `profile_fingerprint` уже включает `phone` и `contact_email`
  (`app/matching/bindings.py:18`) — теперь это корректно: поля реально влияют на
  вывод. Никаких изменений фингерпринта.

### 4.2. `app/profiles/service.py` — `ResumeService`

Новый метод:

```python
class ResumeInUseError(RuntimeError):
    """Резюме используется в отклике или оценке и не может быть удалено."""


async def delete(self, session: AsyncSession, resume_id: UUID) -> None:
    resume = await session.get(Resume, resume_id)
    if resume is None:
        raise LookupError(f"resume {resume_id} does not exist")
    app_refs = await session.scalar(
        select(func.count(Application.id)).where(Application.resume_id == resume_id)
    )
    eval_refs = await session.scalar(
        select(func.count(MatchEvaluation.id)).where(MatchEvaluation.resume_id == resume_id)
    )
    if app_refs or eval_refs:
        raise ResumeInUseError("resume is referenced by an application or evaluation")
    await session.delete(resume)
    await session.flush()
```

Файл на диске отвязывает **вызывающий маршрут после успешного commit**
(best-effort, `safe_storage_path(...).unlink(missing_ok=True)`, кроме
плейсхолдеров `storage_key.startswith("pending/")`). Это исключает окно
«файл удалён, а строка вернулась после сбоя commit».

`deactivate()` и `activate()` уже реализованы и переиспользуются как есть
(`deactivate` сбрасывает `is_default`; `activate` требует наличие файла).

### 4.3. `app/admin/routes.py` — маршруты панели

| Маршрут | Изменение |
|---|---|
| `POST /admin/profiles` | Расширить: `name`, `make_default`, плюс опционально `resume_name`, `resume_category`, `resume_file: UploadFile \| None = File(None)`. Если пришёл файл (`resume_file and resume_file.filename`) — `resume_name` и `resume_category` обязательны, иначе `422`. Порядок: `validate_resume_upload` (чистая, без I/O) → `ProfileService.create_profile` (профиль + строка `JobPreference`) → `ResumeService.upload(..., make_default=True)` → `session.commit()`. При исключении после записи файла — best-effort `unlink`. Редирект `?view=settings&profile_id={new}&notice=profile_created` (или `profile_and_resume_created`). Файл-хелпер общий с `admin_upload_resume`. |
| `GET /admin/resumes/{id}/file` | Новый. `Depends(require_admin_page)`. Проверка `resume.profile_id == selected_profile.id` иначе `404` (паритет с `verify_resume`). Плейсхолдер `pending/` → `404`. Тело: `read_verified_resume(root, storage_key, expected_sha256=resume.sha256, expected_mime_type=resume.mime_type, max_bytes=settings.max_resume_bytes)` → `Response(content=..., media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{ascii_safe}"'})`. `Cache-Control: no-store`. |
| `POST /admin/resumes/{id}/deactivate` | Новый. CSRF. `ResumeService.deactivate`. Аудит `resume.deactivated`. Редирект в настройки. |
| `POST /admin/resumes/{id}/activate` | Новый. CSRF. `ResumeService.activate`; `ValueError` (нет файла) → `422`. Аудит `resume.activated`. |
| `POST /admin/resumes/{id}/delete` | Новый. CSRF. Проверка принадлежности профилю → `404`. `ResumeService.delete`; `ResumeInUseError` → `409`. После commit — unlink файла. Аудит `resume.deleted` (`details={"sha256": ...}`). |

Без изменений: `POST /admin/profile`, `POST /admin/resumes`,
`POST /admin/resumes/{id}/verify`, `POST /admin/profiles/{id}/default`,
`POST /admin/pause/{paused}`.

Добавить русские метки новых действий в `_audit_action_label`
(`resume.deactivated` → «Резюме деактивировано», `resume.activated` →
«Резюме активировано», `resume.deleted` → «Резюме удалено»).

### 4.4. `app/admin/routes.py` — контекст `dashboard()` (ветка `view == "settings"`)

Список `resumes` уже отфильтрован по `selected_profile_id`. Добавить флаг
использования одним запросом на таблицу (не N+1):

```python
resume_ids = [r.id for r in resumes]
used = set()
if resume_ids:
    used |= set((await session.scalars(
        select(Application.resume_id).where(Application.resume_id.in_(resume_ids))
    )).all())
    used |= set((await session.scalars(
        select(MatchEvaluation.resume_id).where(MatchEvaluation.resume_id.in_(resume_ids))
    )).all())
resume_usage = {rid: (rid in used) for rid in resume_ids}
```

Передать `resume_usage` в контекст. Остальное (`profile`, `profiles`,
`selected_profile_id`, `preferences`, `sources`) уже есть.

### 4.5. `app/api/routes.py` и `app/mcp/server.py` — паритет

- REST: `DELETE /api/v1/resumes/{id}`, `POST /api/v1/resumes/{id}/activate`,
  `POST /api/v1/resumes/{id}/deactivate` — через те же методы `ResumeService`,
  тот же аудит (`record_audit_event`). `DELETE` при ссылках → `409`.
- MCP: новый тул `delete_resume(resume_id)` — та же проверка «не используется»,
  аудит `resume.deleted`. `activate_resume` / `deactivate_resume` уже есть.

### 4.6. `app/admin/templates/dashboard_settings.html` — раскладка

Порядок блоков:

1. **Gmail-карточка** — без изменений.
2. **Блок «Профиль»** (панель, для выбранного профиля):
   - Заголовок + имя профиля + один блочный `<details>` **«＋ Новый профиль»**:
     поля `имя` (required), `сделать основным` (switch), подсекция
     «Первое резюме (необязательно)»: `название`, `категория`, `файл PDF`.
     `action="/admin/profiles"`, `enctype="multipart/form-data"`.
   - Форма полей профиля (`action="/admin/profile"`): имя, контактный email,
     телефон, город, языки, навыки. Подсказки `field-help` под email и
     телефоном: «Попадёт в подпись письма работодателю».
   - **«Резюме этого профиля»** — единый паттерн «список + строка добавления»:
     - строка резюме: `название · категория · original_filename` + бейдж
       (`Подтверждено` при `verified and active`; `Проверьте` при `not verified`;
       `Неактивно` при `verified and not active`) + действия:
       - `Открыть` — `<a href="/admin/resumes/{id}/file" target="_blank" rel="noopener">`;
         скрыт для плейсхолдеров `pending/`;
       - `Подтвердить` — форма `POST /admin/resumes/{id}/verify` (если `not verified`);
       - `Деактивировать` / `Активировать` — форма на соответствующий маршрут;
       - `Удалить` — форма `POST /admin/resumes/{id}/delete` c `data-confirm`;
         **рендерится только если `not resume_usage[item.id]`**;
     - всегда видимая компактная строка добавления (не `<details>`):
       `название`, `категория`, `файл`, `основное` (switch), кнопка «Загрузить».
       `action="/admin/resumes"`.
   - Ссылка «Критерии поиска для этого профиля ↓» (якорь к блоку критериев).
3. **«Критерии и лимиты»** — контент без изменений, отдельный блок сразу после
   «Профиля»; выровнять классы под общий вид.
4. **«Источники вакансий»** — `<details>`, поведение без изменений; разметку
   строк/действий привести к тому же паттерну, что список резюме.

Ограничения панели соблюдаются: раскрытие — только `<details>`/`<summary>`
(инлайновый JS запрещён строгим CSP в `app/main.py`); действия-подтверждения —
через существующий `admin.js` (`data-confirm`, `data-confirm-tone`). Ноль
`<details>` внутри `<details>`.

## 5. Модель данных

Изменений нет. Ни новых колонок, ни enum-значений, ни миграции. `alembic check`
в CI должен оставаться зелёным без новой ревизии.

## 6. Обработка ошибок

| Случай | Поведение |
|---|---|
| Комбинированное создание: файл без `resume_name`/`resume_category` | `422`, профиль не создан |
| Комбинированное создание: битый PDF | `UnsafeResumeError` → `422` с сообщением, транзакция откачена, файл не записан |
| Комбинированное создание: сбой БД после записи файла | Откат транзакции + best-effort `unlink` записанного файла |
| `DELETE`/`/delete` используемого резюме | `409` (в панели кнопка и так скрыта) |
| `/activate` резюме без файла на диске | `422` (существующее поведение `ResumeService.activate`) |
| `GET /file` для плейсхолдера `pending/` | `404`, в UI ссылка «Открыть» не рендерится |
| `GET /file` / `/delete` / `/activate` для резюме чужого профиля | `404` (паритет с `verify_resume`) |
| Деактивация последнего active+verified резюме | Разрешена; сервер не блокирует; UI показывает предупреждение, что подготовка заявок остановится |
| Любая мутация без CSRF-токена | `403` (существующий `require_csrf`) |

## 7. Безопасность

- **Просмотр резюме** доступен только аутентифицированной admin-сессии
  (`require_admin_page`). Оператор сам загрузил файл; отдать его обратно — не
  новое раскрытие. MCP и REST-bearer этот маршрут не получают. Обосновать в
  `docs/security.md`.
- `read_verified_resume` сохраняет свои проверки (path-safety, `O_NOFOLLOW`,
  границы размера, сигнатура `%PDF-`, совпадение sha256 с сохранёнными
  метаданными) — при просмотре тоже.
- `Content-Disposition: inline` + `Content-Type: application/pdf` +
  `X-Content-Type-Options: nosniff` (уже ставит middleware). Имя файла в
  заголовке — ASCII-safe.
- Телефон/почта в подписи — данные из профиля оператора, не из недоверенного
  текста вакансии и не из LLM. `detect_prompt_injection(body)` всё равно
  прогоняется по финальному телу.
- Жёсткое удаление невозможно, пока резюме связано с историей отклика/оценки —
  аудитная цепочка `Application → resume_id` не рвётся.
- Все мутации (`resume.deactivated/activated/deleted`, `profile.created`)
  пишут `AuditEvent`.
- Дельта threat-model: нового класса угроз не вводится (T-таблицу расширять не
  нужно); зафиксировать в `docs/security.md` обоснование admin-only просмотра.

## 8. Интеграция с существующим кодом

- `generate_letter` — единственная правка бизнес-логики; всё остальное —
  транспорт (маршруты, шаблон) и один метод сервиса.
- `prepare()` перегенерирует тело письма при каждом прогоне
  `prepare_pending_applications` для не-терминальных заявок по существующим
  правилам; форсированной миграции старых писем не добавляем.
- `POLICY_VERSION` не меняется (правила policy engine не тронуты).
- `choose_resume_for_job` / `select_for_job` / `select_for_category` уже
  фильтруют `active & verified` — деактивация резюме автоматически выводит его
  из автоотправки без отдельного кода.

## 9. Тестирование

**Unit:**
- `tests/unit/test_application_generation.py`: подпись содержит телефон/почту,
  когда заполнены; строку не добавляет, когда пусто; ru/ro/en; при обоих пустых
  — тело байт-в-байт прежнее; `content_validated` остаётся `True`.
- `ResumeService.delete`: удаляет неиспользуемое резюме (строка исчезла);
  `ResumeInUseError` при ссылке из `Application`; то же при ссылке из
  `MatchEvaluation`.
- Комбинированное создание: только профиль (без файла) — как раньше;
  профиль + валидное резюме → оба созданы, резюме `is_default=True`; файл без
  имени/категории → `422`; битый PDF → `422` + профиль не создан.

**Integration / admin** (`tests/unit/test_admin_ui.py`,
`tests/integration/test_interfaces.py`):
- `GET /admin/resumes/{id}/file` → `200`, `application/pdf`, байты совпадают с
  загруженными; без сессии → редирект на логин; плейсхолдер → `404`; чужой
  профиль → `404`.
- `deactivate` / `activate` / `delete` round-trip: статус меняется, аудит
  записан, CSRF обязателен (`403` без токена).
- `delete` используемого резюме → `409`.
- Рендер страницы настроек: блок «Профиль», строки резюме с корректным набором
  действий для каждого состояния, кнопка «Удалить» отсутствует для используемого
  резюме, «＋ Новый профиль» с полем файла.
- REST `DELETE /api/v1/resumes/{id}` + `activate`/`deactivate`; MCP
  `delete_resume` — «не используется» и `409`/ошибка при ссылках.

**Playwright** (гейт AGENTS.md — чистый контекст браузера, 3 зелёных прогона
подряд): создать профиль + резюме одной формой → попасть на новый профиль →
открыть PDF в новой вкладке → деактивировать → активировать → загрузить второе
резюме → удалить первое (неиспользуемое) → подтвердить.

**Проверки перед хендоффом:** `ruff check .`, `ruff format --check .`,
`mypy app fixture_site`, `pytest`, `alembic check` (без новой ревизии),
`docker compose config --quiet`.

**Документация:** `README.md` (разделы «Профиль, резюме и пожелания» — подпись
письма, просмотр/деактивация/удаление резюме, создание профиля с резюме);
`docs/security.md` (обоснование admin-only просмотра резюме).

## 10. Открытые вопросы

- **Уровень языка (`level`) через панель.** Сейчас ввод «ru, ro» сохраняется как
  `[{"code": "ru", "confirmed": true}, …]` — уровень теряется. Не входит в объём;
  можно добавить отдельным мелким изменением позже (поле «язык — уровень» парой).
- **Строка добавления резюме — всегда видимая или под `<details>`.** Дизайн
  предполагает всегда видимую компактную строку ради консистентности; если при
  многих резюме это визуально шумно — тривиально свернуть в блочный `<details>`.
- **Формат телефона в подписи.** По умолчанию — как ввёл оператор. Опционально
  прогонять через `app/phone/numbers.normalize_e164` для единообразия отображения.

Значения и решения выше — предложение; оператор может скорректировать при ревью.
