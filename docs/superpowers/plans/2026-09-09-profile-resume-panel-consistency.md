# Profile & Resume Panel Consistency — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the admin `Настройки → Профиль/Резюме` area consistent — contact
fields flow into the application letter signature; resumes gain view,
deactivate/reactivate and conditional delete in the panel and via REST/MCP; a
profile can be created together with its first resume in one form; the section is
one uniform "Профиль" block.

**Architecture:** One business-logic change (`generate_letter` appends contact
lines to the signature). Everything else is transport: five admin routes (one
extended, four new), three REST endpoints, one MCP tool, one service method
(`ResumeService.delete`), and a template restructure. No schema migration.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Jinja2 templates,
pytest / pytest-asyncio, httpx `ASGITransport`, Playwright (opt-in), `uv`.

**Spec:** `docs/superpowers/specs/2026-09-09-profile-resume-panel-consistency-design.md`

## Global Constraints

- Python 3.12 semantics. Before hand-off run `ruff check .`,
  `ruff format --check .`, `mypy app fixture_site`, `pytest`.
- Never enable real email delivery or live crawling in tests.
- Develop and validate only in the local WSL checkout. Do not touch production,
  `/srv/jobhunter-prod`, `/srv/phonegate`, or any Docker/compose project.
- No database migration is introduced by this plan; `alembic check` must stay
  green with no new revision.
- Repo convention: docs in Russian, commit messages in English.
- End every commit message with:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01VRsTdpBMwEfvEVGofYJCib
  ```
- Strict CSP (`app/main.py`) forbids inline `<script>`; disclosure in templates
  uses `<details>`/`<summary>` only; confirm dialogs use existing `admin.js`
  (`data-confirm`, `data-confirm-tone`, `data-confirm-action`).
- Letter body with both contact fields empty must be **byte-identical** to today.
- `phone` / `contact_email` are not "claims": `all_claims_confirmed`,
  `used_confirmed_facts`, `confirmed_facts`, `POLICY_VERSION` are untouched.
- Hard resume delete is allowed only when no `Application` and no
  `MatchEvaluation` reference the resume (both FKs are `ondelete="RESTRICT"`).
- Work is on branch `feature/profile-resume-panel` (already created; the spec is
  committed there).

---

## File Structure

**Modify:**

- `app/applications/service.py` — `generate_letter`: extract `_letter_signature`,
  append localized `Тел.`/`Email` lines.
- `app/profiles/service.py` — add `ResumeInUseError`; add `ResumeService.delete`.
- `app/admin/routes.py` — extend `create_profile` (`POST /admin/profiles`); add
  `GET /admin/resumes/{id}/file`, `POST /admin/resumes/{id}/deactivate`,
  `POST /admin/resumes/{id}/activate`, `POST /admin/resumes/{id}/delete`; add
  `resume_usage` to the `view == "settings"` context; extend `_AUDIT_ACTION_LABELS`
  and `_FEEDBACK_NOTICES`.
- `app/admin/templates/dashboard_settings.html` — restructure into one "Профиль"
  block with a uniform list pattern and no nested `<details>`.
- `app/api/routes.py` — add `DELETE /api/v1/resumes/{id}`,
  `POST /api/v1/resumes/{id}/activate`, `POST /api/v1/resumes/{id}/deactivate`.
- `app/mcp/server.py` — add `delete_resume` tool.
- `README.md` — profile/resume section: signature, view/deactivate/delete,
  combined create, in-flight-letter limitation.
- `docs/security.md` — rationale for admin-only resume view.

**Test (all modify):**

- `tests/unit/test_application_generation.py`
- `tests/integration/test_interfaces.py`

---

### Task 1: Contact lines in the letter signature

**Files:**
- Modify: `app/applications/service.py` (function `generate_letter`, ~lines 89-130)
- Test: `tests/unit/test_application_generation.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `generate_letter(profile: UserProfile, job: SourceJob) -> tuple[str, str, str, list[str]]`
  — unchanged signature. Behaviour change only: `body` ends with a signature
  block that appends `phone` then `contact_email` lines when those profile
  fields are non-empty (after `str.strip()`), localized per letter language.

- [ ] **Step 1: Write the failing signature tests**

Add to `tests/unit/test_application_generation.py`:

```python
def _ru_profile(**overrides: object) -> UserProfile:
    base: dict[str, object] = {
        "name": "Кандидат",
        "languages": [{"code": "ru", "confirmed": True}],
    }
    base.update(overrides)
    return UserProfile(**base)  # type: ignore[arg-type]


def test_letter_signature_appends_phone_and_email_when_present() -> None:
    profile = _ru_profile(phone="+373 60 000 000", contact_email="cv@example.com")

    _subject, body, _language, _facts = generate_letter(profile, _job("ru"))

    assert body.endswith(
        "С уважением,\nКандидат\nТел.: +373 60 000 000\nEmail: cv@example.com"
    )


def test_letter_signature_omits_blank_contact_lines() -> None:
    profile = _ru_profile(phone="   ", contact_email=None)

    _subject, body, _language, _facts = generate_letter(profile, _job("ru"))

    assert body.endswith("С уважением,\nКандидат")
    assert "Тел." not in body
    assert "Email" not in body


def test_letter_signature_is_localised() -> None:
    ro_profile = _ru_profile(
        languages=[{"code": "ro", "confirmed": True}],
        phone="+373 1",
        contact_email="a@b.co",
    )
    _s, ro_body, ro_lang, _f = generate_letter(ro_profile, _job("ro"))
    assert ro_lang == "ro"
    assert ro_body.endswith("Cu respect,\nКандидат\nTel.: +373 1\nEmail: a@b.co")

    en_profile = _ru_profile(
        languages=[{"code": "en", "confirmed": True}],
        phone="+373 2",
        contact_email="c@d.co",
    )
    _s2, en_body, en_lang, _f2 = generate_letter(en_profile, _job("en"))
    assert en_lang == "en"
    assert en_body.endswith("Kind regards,\nКандидат\nPhone: +373 2\nEmail: c@d.co")


def test_letter_body_is_byte_identical_without_contact_fields() -> None:
    profile = _ru_profile()

    _subject, body, _language, _facts = generate_letter(profile, _job("ru"))

    assert body == (
        "Здравствуйте, команда Example!\n\n"
        "Хочу откликнуться на вакансию «Python Developer». "
        "Буду рад обсудить требования и формат работы.\n\n"
        "С уважением,\nКандидат"
    )


def test_letter_with_contact_lines_is_not_flagged_as_prompt_injection() -> None:
    from app.crawlers.parsing.normalization import detect_prompt_injection

    profile = _ru_profile(phone="+373 60 000 000", contact_email="cv@example.com")
    _subject, body, _language, _facts = generate_letter(profile, _job("ru"))

    assert detect_prompt_injection(body) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_application_generation.py -q`
Expected: the five new tests FAIL (`body` currently ends `С уважением,\nКандидат`
with no contact lines; `_ru_profile` helper is new so `NameError` is also fine
before the helper is added — add the helper in Step 1).

- [ ] **Step 3: Extract the signature helper**

In `app/applications/service.py`, above `def generate_letter(`, add:

```python
_SIGNATURE_CLOSERS = {
    "ru": "С уважением,",  # noqa: RUF001 - intentional Cyrillic text
    "ro": "Cu respect,",
    "en": "Kind regards,",
}
_SIGNATURE_CONTACT_LABELS = {
    "ru": ("Тел.: ", "Email: "),  # noqa: RUF001 - intentional Cyrillic text
    "ro": ("Tel.: ", "Email: "),
    "en": ("Phone: ", "Email: "),
}


def _letter_signature(profile: UserProfile, language: str) -> str:
    lines = [_SIGNATURE_CLOSERS[language], profile.name]
    phone_label, email_label = _SIGNATURE_CONTACT_LABELS[language]
    phone = (profile.phone or "").strip()
    email = (profile.contact_email or "").strip()
    if phone:
        lines.append(f"{phone_label}{phone}")
    if email:
        lines.append(f"{email_label}{email}")
    return "\n".join(lines)
```

- [ ] **Step 4: Use the helper in all three language branches**

In `generate_letter`, replace the trailing closer fragment of each `body = (...)`
literal:

- ru branch: replace `f"С уважением,\n{profile.name}"  # noqa: RUF001 - intentional Cyrillic text`
  with `f"{_letter_signature(profile, language)}"`
- ro branch: replace `f"Cu respect,\n{profile.name}"` with `f"{_letter_signature(profile, language)}"`
- en branch: replace `f"Kind regards,\n{profile.name}"` with `f"{_letter_signature(profile, language)}"`

Leave `subject`, `relevance`, greeting, and body text untouched.

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
uv run pytest tests/unit/test_application_generation.py -q
uv run ruff check app/applications/service.py tests/unit/test_application_generation.py
uv run ruff format --check app/applications/service.py tests/unit/test_application_generation.py
uv run mypy app fixture_site
```
Expected: all pass. If `ruff` reports `RUF001` on `app/applications/service.py`,
confirm the `# noqa: RUF001` comments are on the two dict lines that contain
Cyrillic string literals.

- [ ] **Step 6: Commit**

```bash
git add app/applications/service.py tests/unit/test_application_generation.py
git commit -m "feat: add candidate contact lines to the application letter signature"
```

---

### Task 2: `ResumeService.delete` with reference guard

**Files:**
- Modify: `app/profiles/service.py`
- Test: `tests/integration/test_interfaces.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `class ResumeInUseError(RuntimeError)` in `app/profiles/service.py`.
  - `ResumeService.delete(self, session: AsyncSession, resume_id: UUID) -> str | None`
    — raises `LookupError` if the resume row is absent; raises `ResumeInUseError`
    if any `Application.resume_id == resume_id` or `MatchEvaluation.resume_id ==
    resume_id`; otherwise `session.delete(resume)` + `session.flush()` and returns
    the `storage_key` to unlink after commit, or `None` when the key starts with
    `"pending/"` (placeholder, no file on disk).

- [ ] **Step 1: Write the failing service test**

Add to `tests/integration/test_interfaces.py` (near the other `ResumeService`
coverage). It needs `sqlite_session_factory` and the existing
`_seed_review_application` helper:

```python
@pytest.mark.asyncio
async def test_resume_service_delete_guards_referenced_resumes(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    from app.profiles.service import ResumeInUseError, ResumeService

    _application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory, settings, suffix="resume-delete-guard"
    )

    async with sqlite_session_factory() as session:
        with pytest.raises(ResumeInUseError):
            await ResumeService(settings).delete(session, seeded["resume_id"])

    async with sqlite_session_factory() as session:
        still_there = await session.get(Resume, seeded["resume_id"])
        assert still_there is not None


@pytest.mark.asyncio
async def test_resume_service_delete_removes_unreferenced_resume_and_file(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    from app.profiles.service import ResumeService

    _application, settings = interface_app
    settings.resume_storage_path.mkdir(parents=True, exist_ok=True)
    storage_key = "orphan-resume.pdf"
    resume_path = settings.resume_storage_path / storage_key
    resume_path.write_bytes(b"%PDF-1.4\norphan\n%%EOF")

    async with sqlite_session_factory() as session:
        profile = UserProfile(name="Orphan owner", is_default=True)
        session.add(profile)
        await session.flush()
        resume = Resume(
            profile_id=profile.id,
            name="Orphan",
            category="ops",
            storage_key=storage_key,
            original_filename="orphan.pdf",
            mime_type="application/pdf",
            sha256=hashlib.sha256(b"%PDF-1.4\norphan\n%%EOF").hexdigest(),
            active=True,
            verified=False,
            is_default=False,
        )
        session.add(resume)
        await session.commit()
        resume_id = resume.id

    async with sqlite_session_factory() as session:
        unlink_key = await ResumeService(settings).delete(session, resume_id)
        await session.commit()

    assert unlink_key == storage_key
    async with sqlite_session_factory() as session:
        assert await session.get(Resume, resume_id) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_interfaces.py -q -k resume_service_delete`
Expected: FAIL — `ResumeInUseError` / `ResumeService.delete` do not exist yet
(`ImportError` / `AttributeError`).

- [ ] **Step 3: Add the exception and imports**

In `app/profiles/service.py`:

- change `from sqlalchemy import select, update` to
  `from sqlalchemy import func, select, update`
- change `from app.models.entities import JobPreference, Resume, SourceJob, UserProfile`
  to `from app.models.entities import (Application, JobPreference, MatchEvaluation, Resume, SourceJob, UserProfile)`
- add, above `class ProfileService:`:

```python
class ResumeInUseError(RuntimeError):
    """A resume referenced by an application or evaluation cannot be deleted."""
```

- [ ] **Step 4: Implement `ResumeService.delete`**

Add to `class ResumeService` (after `deactivate`):

```python
async def delete(self, session: AsyncSession, resume_id: UUID) -> str | None:
    resume = await session.get(Resume, resume_id)
    if resume is None:
        raise LookupError(f"resume {resume_id} does not exist")
    application_refs = await session.scalar(
        select(func.count()).select_from(Application).where(Application.resume_id == resume_id)
    )
    evaluation_refs = await session.scalar(
        select(func.count())
        .select_from(MatchEvaluation)
        .where(MatchEvaluation.resume_id == resume_id)
    )
    if application_refs or evaluation_refs:
        raise ResumeInUseError("resume is referenced and can only be deactivated")
    storage_key = resume.storage_key
    await session.delete(resume)
    await session.flush()
    return None if storage_key.startswith("pending/") else storage_key
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
uv run pytest tests/integration/test_interfaces.py -q -k resume_service_delete
uv run ruff check app/profiles/service.py
uv run mypy app fixture_site
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/profiles/service.py tests/integration/test_interfaces.py
git commit -m "feat: add ResumeService.delete with application and evaluation guard"
```

---

### Task 3: Admin route — view resume PDF

**Files:**
- Modify: `app/admin/routes.py`
- Test: `tests/integration/test_interfaces.py`

**Interfaces:**
- Consumes: `read_verified_resume`, `UnsafeResumeError` from `app.security.files`;
  `ProfileService.get_profile`.
- Produces: `GET /admin/resumes/{resume_id}/file?profile_id=<uuid>` — behind
  `require_admin_page`; returns `200` `application/pdf` inline for a verified or
  unverified resume owned by the selected profile; `404` when the resume is
  missing, owned by another profile, a `pending/` placeholder, or the file cannot
  be read safely; `303 -> /login` when unauthenticated.

- [ ] **Step 1: Write the failing route tests**

Add to `tests/integration/test_interfaces.py`:

```python
@pytest.mark.asyncio
async def test_admin_resume_file_view_returns_pdf_bytes(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    pdf = b"%PDF-1.7\nviewable resume\n%%EOF"
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="Viewer", is_default=True)
        session.add(profile)
        await session.flush()
        profile_id = profile.id

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as client:
        csrf_token = await _login_admin(client, settings)
        upload = await client.post(
            "/admin/resumes",
            data={
                "profile_id": str(profile_id),
                "name": "Viewable",
                "category": "ops",
                "csrf_token": csrf_token,
            },
            files={"file": ("viewable.pdf", pdf, "application/pdf")},
        )
        assert upload.status_code == 303

        async with sqlite_session_factory() as session:
            resume = await session.scalar(select(Resume).where(Resume.name == "Viewable"))
            assert resume is not None
            resume_id = resume.id

        view = await client.get(
            f"/admin/resumes/{resume_id}/file", params={"profile_id": str(profile_id)}
        )
        assert view.status_code == 200
        assert view.headers["content-type"] == "application/pdf"
        assert view.headers["content-disposition"].startswith("inline")
        assert view.content == pdf

    unauth_transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=unauth_transport, base_url="https://testserver", follow_redirects=False
    ) as anon:
        blocked = await anon.get(f"/admin/resumes/{resume_id}/file")
        assert blocked.status_code == 303
        assert blocked.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_admin_resume_file_view_404_for_placeholder_and_foreign_profile(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    async with sqlite_session_factory() as session:
        owner = UserProfile(name="Owner", is_default=True)
        other = UserProfile(name="Other")
        session.add_all([owner, other])
        await session.flush()
        placeholder = Resume(
            profile_id=owner.id,
            name="Pending",
            category="ops",
            storage_key="pending/deadbeef",
            original_filename="pending.pdf",
            mime_type="application/pdf",
            sha256="0" * 64,
            active=False,
            verified=False,
            is_default=False,
        )
        session.add(placeholder)
        await session.commit()
        owner_id, other_id, placeholder_id = owner.id, other.id, placeholder.id

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as client:
        await _login_admin(client, settings)
        placeholder_view = await client.get(
            f"/admin/resumes/{placeholder_id}/file", params={"profile_id": str(owner_id)}
        )
        assert placeholder_view.status_code == 404
        foreign_view = await client.get(
            f"/admin/resumes/{placeholder_id}/file", params={"profile_id": str(other_id)}
        )
        assert foreign_view.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_interfaces.py -q -k admin_resume_file_view`
Expected: FAIL with `404` for the happy path (route not registered).

- [ ] **Step 3: Add the security-files import**

In `app/admin/routes.py`, after the existing `from app.security.auth import ...`
line, add:

```python
from app.security.files import UnsafeResumeError, read_verified_resume
```

- [ ] **Step 4: Implement the route**

Add near `verify_resume` in `app/admin/routes.py`:

```python
@router.get("/admin/resumes/{resume_id}/file")
async def admin_resume_file(
    resume_id: UUID,
    request: Request,
    profile_id: UUID | None = None,
    _: str = Depends(require_admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    resume = await session.get(Resume, resume_id)
    selected_profile = await ProfileService().get_profile(session, profile_id)
    if resume is None or selected_profile is None or resume.profile_id != selected_profile.id:
        raise HTTPException(status_code=404)
    if resume.storage_key.startswith("pending/"):
        raise HTTPException(status_code=404, detail="resume file has not been uploaded")
    settings = get_settings()
    try:
        data = read_verified_resume(
            settings.resume_storage_path,
            resume.storage_key,
            expected_sha256=resume.sha256,
            expected_mime_type=resume.mime_type,
            max_bytes=settings.max_resume_bytes,
        )
    except UnsafeResumeError as exc:
        raise HTTPException(status_code=404, detail="resume file is unavailable") from exc
    filename = quote(resume.original_filename)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{filename}",
            "Cache-Control": "no-store",
        },
    )
```

(`quote` is already imported from `urllib.parse`; `Response` from
`fastapi.responses`; `get_session`, `ProfileService`, `Resume` already imported.)

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
uv run pytest tests/integration/test_interfaces.py -q -k admin_resume_file_view
uv run ruff check app/admin/routes.py
uv run mypy app fixture_site
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/admin/routes.py tests/integration/test_interfaces.py
git commit -m "feat: let the admin panel open a resume PDF in a new tab"
```

---

### Task 4: Admin routes — deactivate, activate, delete

**Files:**
- Modify: `app/admin/routes.py`
- Test: `tests/integration/test_interfaces.py`

**Interfaces:**
- Consumes: `ResumeService.deactivate`, `ResumeService.activate`,
  `ResumeService.delete`, `ResumeInUseError` (Task 2), `safe_storage_path`,
  `UnsafeResumeError`.
- Produces:
  - `POST /admin/resumes/{resume_id}/deactivate` — CSRF; `303 -> /?view=settings&profile_id=&notice=resume_deactivated`.
  - `POST /admin/resumes/{resume_id}/activate` — CSRF; `422` if the binary is
    missing (`ResumeService.activate` raises `ValueError`); else `303 ... notice=resume_activated`.
  - `POST /admin/resumes/{resume_id}/delete` — CSRF; `409` on `ResumeInUseError`;
    else deletes row + unlinks file after commit; `303 ... notice=resume_deleted`.
  - All three: `404` when the resume is missing or owned by another profile.
  - `resume_usage: dict[UUID, bool]` added to the `view == "settings"` template
    context — `True` when the resume is referenced by an `Application` or
    `MatchEvaluation`.
  - New audit actions `resume.deactivated`, `resume.activated`, `resume.deleted`
    and `profile.created` in `_AUDIT_ACTION_LABELS`; new feedback keys
    `resume_deactivated`, `resume_activated`, `resume_deleted` in
    `_FEEDBACK_NOTICES`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/integration/test_interfaces.py`:

```python
@pytest.mark.asyncio
async def test_admin_resume_deactivate_activate_delete_lifecycle(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="Lifecycle", is_default=True)
        session.add(profile)
        await session.flush()
        profile_id = profile.id

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as client:
        csrf_token = await _login_admin(client, settings)
        await client.post(
            "/admin/resumes",
            data={
                "profile_id": str(profile_id),
                "name": "Lifecycle CV",
                "category": "ops",
                "make_default": "on",
                "csrf_token": csrf_token,
            },
            files={"file": ("cv.pdf", b"%PDF-1.7\ncv\n%%EOF", "application/pdf")},
        )
        async with sqlite_session_factory() as session:
            resume = await session.scalar(select(Resume).where(Resume.name == "Lifecycle CV"))
            assert resume is not None
            resume_id = resume.id

        no_csrf = await client.post(f"/admin/resumes/{resume_id}/deactivate", data={})
        assert no_csrf.status_code == 403

        deactivated = await client.post(
            f"/admin/resumes/{resume_id}/deactivate",
            data={"profile_id": str(profile_id), "csrf_token": csrf_token},
        )
        assert deactivated.status_code == 303
        async with sqlite_session_factory() as session:
            row = await session.get(Resume, resume_id)
            assert row is not None and row.active is False and row.is_default is False

        activated = await client.post(
            f"/admin/resumes/{resume_id}/activate",
            data={"profile_id": str(profile_id), "csrf_token": csrf_token},
        )
        assert activated.status_code == 303
        async with sqlite_session_factory() as session:
            row = await session.get(Resume, resume_id)
            assert row is not None and row.active is True

        deleted = await client.post(
            f"/admin/resumes/{resume_id}/delete",
            data={"profile_id": str(profile_id), "csrf_token": csrf_token},
        )
        assert deleted.status_code == 303
        async with sqlite_session_factory() as session:
            assert await session.get(Resume, resume_id) is None
            actions = set(
                (await session.scalars(select(AuditEvent.action))).all()
            )
            assert {"resume.deactivated", "resume.activated", "resume.deleted"} <= actions
        assert not (settings.resume_storage_path / "cv.pdf").exists()


@pytest.mark.asyncio
async def test_admin_resume_delete_conflicts_when_referenced(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory, settings, suffix="admin-delete-conflict"
    )

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as client:
        csrf_token = await _login_admin(client, settings)
        conflict = await client.post(
            f"/admin/resumes/{seeded['resume_id']}/delete",
            data={"profile_id": str(seeded["profile_id"]), "csrf_token": csrf_token},
        )
        assert conflict.status_code == 409

    async with sqlite_session_factory() as session:
        assert await session.get(Resume, seeded["resume_id"]) is not None


@pytest.mark.asyncio
async def test_admin_resume_activate_422_when_binary_missing(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="No binary", is_default=True)
        session.add(profile)
        await session.flush()
        placeholder = Resume(
            profile_id=profile.id,
            name="Placeholder",
            category="ops",
            storage_key="pending/cafebabe",
            original_filename="p.pdf",
            mime_type="application/pdf",
            sha256="0" * 64,
            active=False,
            verified=False,
            is_default=False,
        )
        session.add(placeholder)
        await session.commit()
        profile_id, resume_id = profile.id, placeholder.id

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as client:
        csrf_token = await _login_admin(client, settings)
        response = await client.post(
            f"/admin/resumes/{resume_id}/activate",
            data={"profile_id": str(profile_id), "csrf_token": csrf_token},
        )
        assert response.status_code == 422
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_interfaces.py -q -k "admin_resume_deactivate or admin_resume_delete_conflicts or admin_resume_activate_422"`
Expected: FAIL (`404` — routes not registered).

- [ ] **Step 3: Extend the label and notice dictionaries**

In `app/admin/routes.py`:

- add to `_AUDIT_ACTION_LABELS` (keep alphabetical grouping):
  ```python
      "profile.created": "Профиль создан",
      "resume.activated": "Резюме снова активно",
      "resume.deactivated": "Резюме деактивировано",
      "resume.deleted": "Резюме удалено",
  ```
- add to `_FEEDBACK_NOTICES`:
  ```python
      "resume_deactivated": (
          "Резюме деактивировано",
          "Оно больше не используется для новых откликов; активировать можно обратно.",
      ),
      "resume_activated": ("Резюме активно", "Оно снова доступно для подготовки откликов."),
      "resume_deleted": ("Резюме удалено", "Файл и запись удалены безвозвратно."),
      "profile_and_resume_created": (
          "Профиль и резюме созданы",
          "Проверьте резюме перед использованием в автоматических откликах.",
      ),
  ```

- [ ] **Step 4: Add `resume_usage` to the settings context**

In `app/admin/routes.py`, in the `elif view == "settings":` block, after the
`resumes = list(...)` assignment, add:

```python
        resume_ids = [item.id for item in resumes]
        referenced: set[UUID] = set()
        if resume_ids:
            referenced |= set(
                (
                    await session.scalars(
                        select(Application.resume_id).where(Application.resume_id.in_(resume_ids))
                    )
                ).all()
            )
            referenced |= set(
                (
                    await session.scalars(
                        select(MatchEvaluation.resume_id).where(
                            MatchEvaluation.resume_id.in_(resume_ids)
                        )
                    )
                ).all()
            )
        resume_usage = {item_id: (item_id in referenced) for item_id in resume_ids}
```

Initialise `resume_usage: dict[UUID, bool] = {}` where `resumes: list[Resume] = []`
is initialised (search for that line, ~line 938), and add
`"resume_usage": resume_usage,` to the `context=` dict passed to
`templates.TemplateResponse` (next to `"resumes": resumes,`).

- [ ] **Step 5: Implement the three routes**

Add after `verify_resume` in `app/admin/routes.py`:

```python
async def _owned_resume(
    session: AsyncSession, resume_id: UUID, profile_id: UUID | None
) -> tuple[Resume, UserProfile]:
    resume = await session.get(Resume, resume_id)
    selected_profile = await ProfileService().get_profile(session, profile_id)
    if resume is None or selected_profile is None or resume.profile_id != selected_profile.id:
        raise HTTPException(status_code=404)
    return resume, selected_profile


@router.post("/admin/resumes/{resume_id}/deactivate")
async def admin_deactivate_resume(
    resume_id: UUID,
    request: Request,
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    _resume, selected_profile = await _owned_resume(session, resume_id, profile_id)
    await ResumeService(get_settings()).deactivate(session, resume_id)
    await _audit_admin(session, "resume.deactivated", "resume", str(resume_id))
    await session.commit()
    return RedirectResponse(
        f"/?view=settings&profile_id={selected_profile.id}&notice=resume_deactivated",
        status_code=303,
    )


@router.post("/admin/resumes/{resume_id}/activate")
async def admin_activate_resume(
    resume_id: UUID,
    request: Request,
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    _resume, selected_profile = await _owned_resume(session, resume_id, profile_id)
    try:
        await ResumeService(get_settings()).activate(session, resume_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit_admin(session, "resume.activated", "resume", str(resume_id))
    await session.commit()
    return RedirectResponse(
        f"/?view=settings&profile_id={selected_profile.id}&notice=resume_activated",
        status_code=303,
    )


@router.post("/admin/resumes/{resume_id}/delete")
async def admin_delete_resume(
    resume_id: UUID,
    request: Request,
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    _resume, selected_profile = await _owned_resume(session, resume_id, profile_id)
    settings = get_settings()
    try:
        unlink_key = await ResumeService(settings).delete(session, resume_id)
    except ResumeInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _audit_admin(session, "resume.deleted", "resume", str(resume_id))
    await session.commit()
    if unlink_key is not None:
        try:
            safe_storage_path(settings.resume_storage_path, unlink_key).unlink(missing_ok=True)
        except UnsafeResumeError:
            pass
    return RedirectResponse(
        f"/?view=settings&profile_id={selected_profile.id}&notice=resume_deleted",
        status_code=303,
    )
```

Add imports to `app/admin/routes.py`:
- `from app.profiles.service import ResumeInUseError`
- extend the `app.security.files` import from Task 3 to
  `from app.security.files import UnsafeResumeError, read_verified_resume, safe_storage_path`

- [ ] **Step 6: Run the tests to verify they pass**

Run:
```bash
uv run pytest tests/integration/test_interfaces.py -q -k "admin_resume"
uv run ruff check app/admin/routes.py
uv run ruff format --check app/admin/routes.py
uv run mypy app fixture_site
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/admin/routes.py tests/integration/test_interfaces.py
git commit -m "feat: add resume deactivate, activate and delete to the admin panel"
```

---

### Task 5: Admin route — create a profile with its first resume

**Files:**
- Modify: `app/admin/routes.py` (function `create_profile`)
- Test: `tests/integration/test_interfaces.py`

**Interfaces:**
- Consumes: `ProfileService.create_profile`, `ResumeService.upload`,
  `validate_resume_upload`, `UnsafeResumeError`.
- Produces: `POST /admin/profiles` accepts optional `resume_name: str = Form("")`,
  `resume_category: str = Form("")`, `resume_file: UploadFile | None = File(None)`.
  When a file is supplied (`resume_file and resume_file.filename`) `resume_name`
  and `resume_category` are required (`422` otherwise), the resume is uploaded as
  the profile's default in the same transaction, and the redirect notice is
  `profile_and_resume_created`. Without a file the behaviour is exactly as today
  (`notice=profile_created`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/integration/test_interfaces.py`:

```python
@pytest.mark.asyncio
async def test_admin_create_profile_with_first_resume(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as client:
        csrf_token = await _login_admin(client, settings)
        created = await client.post(
            "/admin/profiles",
            data={
                "name": "Courier profile",
                "make_default": "on",
                "resume_name": "Courier CV",
                "resume_category": "courier",
                "csrf_token": csrf_token,
            },
            files={"resume_file": ("courier.pdf", b"%PDF-1.7\ncourier\n%%EOF", "application/pdf")},
        )
        assert created.status_code == 303
        assert "notice=profile_and_resume_created" in created.headers["location"]

    async with sqlite_session_factory() as session:
        profile = await session.scalar(
            select(UserProfile).where(UserProfile.name == "Courier profile")
        )
        assert profile is not None
        resume = await session.scalar(select(Resume).where(Resume.profile_id == profile.id))
        assert resume is not None
        assert resume.name == "Courier CV"
        assert resume.category == "courier"
        assert resume.is_default is True
        assert resume.verified is False
        preference = await session.scalar(
            select(JobPreference).where(JobPreference.profile_id == profile.id)
        )
        assert preference is not None


@pytest.mark.asyncio
async def test_admin_create_profile_without_file_is_unchanged(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as client:
        csrf_token = await _login_admin(client, settings)
        created = await client.post(
            "/admin/profiles",
            data={"name": "Bare profile", "csrf_token": csrf_token},
        )
        assert created.status_code == 303
        assert "notice=profile_created" in created.headers["location"]

    async with sqlite_session_factory() as session:
        profile = await session.scalar(
            select(UserProfile).where(UserProfile.name == "Bare profile")
        )
        assert profile is not None
        assert (
            await session.scalar(select(Resume).where(Resume.profile_id == profile.id))
        ) is None


@pytest.mark.asyncio
async def test_admin_create_profile_rejects_file_without_metadata_and_bad_pdf(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as client:
        csrf_token = await _login_admin(client, settings)

        missing_meta = await client.post(
            "/admin/profiles",
            data={"name": "Needs meta", "csrf_token": csrf_token},
            files={"resume_file": ("x.pdf", b"%PDF-1.7\nx\n%%EOF", "application/pdf")},
        )
        assert missing_meta.status_code == 422

        bad_pdf = await client.post(
            "/admin/profiles",
            data={
                "name": "Bad pdf",
                "resume_name": "CV",
                "resume_category": "ops",
                "csrf_token": csrf_token,
            },
            files={"resume_file": ("x.pdf", b"not a pdf", "application/pdf")},
        )
        assert bad_pdf.status_code == 422

    async with sqlite_session_factory() as session:
        assert (
            await session.scalar(select(UserProfile).where(UserProfile.name == "Needs meta"))
        ) is None
        assert (
            await session.scalar(select(UserProfile).where(UserProfile.name == "Bad pdf"))
        ) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_interfaces.py -q -k admin_create_profile`
Expected: FAIL — extra form fields are ignored, no resume is created, and the
notice is `profile_created` for the combined case.

- [ ] **Step 3: Extend `create_profile`**

Replace the body of `create_profile` in `app/admin/routes.py` with:

```python
@router.post("/admin/profiles")
async def create_profile(
    request: Request,
    name: str = Form(...),
    make_default: bool = Form(False),
    resume_name: str = Form(""),
    resume_category: str = Form(""),
    resume_file: UploadFile | None = File(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    settings = get_settings()
    has_resume = resume_file is not None and bool(resume_file.filename)
    if has_resume and not (resume_name.strip() and resume_category.strip()):
        raise HTTPException(
            status_code=422, detail="resume name and category are required with a file"
        )
    data = b""
    if has_resume:
        assert resume_file is not None
        data = await resume_file.read(settings.max_resume_bytes + 1)
        try:
            validate_resume_upload(
                resume_file.filename or "resume.pdf",
                resume_file.content_type or "",
                data,
                settings.max_resume_bytes,
            )
        except UnsafeResumeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    profile = await ProfileService().create_profile(
        session, UserProfileInput(name=name), make_default=make_default
    )
    await _audit_admin(session, "profile.created", "user_profile", str(profile.id))
    notice = "profile_created"
    if has_resume:
        assert resume_file is not None
        resume = await ResumeService(settings).upload(
            session,
            profile_id=profile.id,
            name=resume_name.strip(),
            category=resume_category.strip(),
            filename=resume_file.filename or "resume.pdf",
            mime_type=resume_file.content_type or "",
            data=data,
            make_default=True,
        )
        await _audit_admin(
            session,
            "resume.uploaded",
            "resume",
            str(resume.id),
            details={"mime_type": resume.mime_type, "sha256": resume.sha256},
        )
        notice = "profile_and_resume_created"
    await session.commit()
    return RedirectResponse(
        f"/?view=settings&profile_id={profile.id}&notice={notice}", status_code=303
    )
```

Ensure `validate_resume_upload` is imported in `app/admin/routes.py`
(`from app.security.files import UnsafeResumeError, read_verified_resume, safe_storage_path, validate_resume_upload`).

Note: `validate_resume_upload` is pure (no I/O). `ResumeService.upload` writes the
file with `open("xb")` before the route's `session.commit()` — identical to the
existing `admin_upload_resume` flow, so no new orphan-file risk is introduced.
`ResumeService.upload` dedups by `(profile_id, sha256)`; for a brand-new profile
that never collides.

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
uv run pytest tests/integration/test_interfaces.py -q -k "admin_create_profile or admin_forms_merge"
uv run ruff check app/admin/routes.py
uv run mypy app fixture_site
```
Expected: all pass (including the pre-existing
`test_admin_forms_merge_unexposed_fields_and_require_explicit_resume`, which still
creates a bare profile-less-resume elsewhere and is unaffected).

- [ ] **Step 5: Commit**

```bash
git add app/admin/routes.py tests/integration/test_interfaces.py
git commit -m "feat: allow creating a profile with its first resume in one form"
```

---

### Task 6: Restructure the settings template into one Профиль block

**Files:**
- Modify: `app/admin/templates/dashboard_settings.html`
- Test: `tests/integration/test_interfaces.py`

**Interfaces:**
- Consumes: `resume_usage` (Task 4); routes from Tasks 3-5; existing context keys
  `profile`, `profiles`, `selected_profile_id`, `preferences`, `sources`,
  `resumes`, `csrf_token`, `gmail_oauth`.
- Produces: a settings page where (1) a single "Профиль" panel holds the profile
  fields form, the profile's resume list with per-row actions, and one
  always-visible add-resume row; (2) "＋ Новый профиль" is one block-level
  `<details>` containing an optional first-resume file input; (3) the resume and
  source lists use the same row+actions markup; (4) no `<details>` nested inside
  another `<details>`.

- [ ] **Step 1: Write the failing render tests**

Add to `tests/integration/test_interfaces.py`:

```python
@pytest.mark.asyncio
async def test_settings_page_renders_unified_profile_block(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory, settings, suffix="settings-render"
    )

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as client:
        csrf_token = await _login_admin(client, settings)
        # a second, unreferenced resume on the same profile
        await client.post(
            "/admin/resumes",
            data={
                "profile_id": str(seeded["profile_id"]),
                "name": "Spare CV",
                "category": "ops",
                "csrf_token": csrf_token,
            },
            files={"file": ("spare.pdf", b"%PDF-1.7\nspare\n%%EOF", "application/pdf")},
        )

        page = await client.get(f"/?view=settings&profile_id={seeded['profile_id']}")
        assert page.status_code == 200
        html = page.text

        # one Профиль block, new-profile disclosure carries a file input
        assert 'action="/admin/profiles"' in html
        assert 'name="resume_file"' in html
        # per-row resume actions
        assert f"/admin/resumes/{seeded['resume_id']}/file?profile_id=" in html
        assert f"/admin/resumes/{seeded['resume_id']}/deactivate" in html
        # referenced resume: no delete form
        assert f"/admin/resumes/{seeded['resume_id']}/delete" not in html

        async with sqlite_session_factory() as session:
            spare = await session.scalar(select(Resume).where(Resume.name == "Spare CV"))
            assert spare is not None
            spare_id = spare.id
        # unreferenced resume: delete form present
        assert f"/admin/resumes/{spare_id}/delete" in html

        # no nested <details> inside <details> (settings view has no <details> in
        # its page shell — only dashboard_settings.html contributes them)
        import re

        depth = 0
        max_depth = 0
        for token in re.findall(r"<details|</details>", html):
            depth += 1 if token == "<details" else -1
            max_depth = max(max_depth, depth)
        assert max_depth <= 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/integration/test_interfaces.py -q -k settings_page_renders_unified`
Expected: FAIL — `name="resume_file"` absent, delete forms not gated on
`resume_usage`, nested `<details>` still present.

- [ ] **Step 3: Rewrite `dashboard_settings.html`**

Replace the "Профиль кандидата" and "Резюме" sections (currently two separate
`<details class="settings-section">` blocks with nested `<details>`) with one
`<div class="panel">` "Профиль" block. Keep the Gmail card, "Критерии и лимиты"
form, and "Источники вакансий" section as they are, except: normalise the source
list rows to the same `compact-row` + actions markup used for resumes, and keep
"Источники" as a single (non-nested) `<details>`.

New "Профиль" block markup:

```html
    <div class="panel">
      <div class="panel-head">
        <div><div class="panel-title">Профиль</div><div class="panel-subtitle">{{ profile.name }} · {{ profile.contact_email or 'почта не указана' }}</div></div>
        <details class="inline-create"><summary class="btn btn-sm">＋ Новый профиль</summary>
          <form class="details-body" method="post" action="/admin/profiles" enctype="multipart/form-data">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <div class="form-grid">
              <label class="field">Название<input type="text" name="name" required></label>
              <label class="switch-row"><span class="switch-copy"><strong>Сделать основным</strong></span><span class="switch"><input type="checkbox" name="make_default"><span class="switch-ui"></span></span></label>
            </div>
            <div class="form-grid">
              <label class="field">Резюме — название<input type="text" name="resume_name"></label>
              <label class="field">Резюме — категория<input type="text" name="resume_category"></label>
              <label class="field full">Резюме — PDF (необязательно)<input type="file" name="resume_file" accept="application/pdf,.pdf"></label>
            </div>
            <button class="btn btn-primary">Создать профиль</button>
          </form>
        </details>
      </div>
      <form method="post" action="/admin/profile" class="panel-pad">
        <input type="hidden" name="profile_id" value="{{ selected_profile_id }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <div class="form-grid">
          <label class="field">Имя<input type="text" name="name" value="{{ profile.name }}" required></label>
          <label class="field">Email<input type="email" name="contact_email" value="{{ profile.contact_email or '' }}"><span class="field-help">Попадёт в подпись письма работодателю</span></label>
          <label class="field">Телефон<input type="text" name="phone" value="{{ profile.phone or '' }}"><span class="field-help">Попадёт в подпись письма — сюда сможет перезвонить HR</span></label>
          <label class="field">Город<input type="text" name="location" value="{{ profile.location or '' }}"></label>
          <label class="field">Языки<input type="text" name="languages" value="{{ profile.languages|map(attribute='code')|join(', ') }}"><span class="field-help">Через запятую</span></label>
          <label class="field">Навыки<input type="text" name="skills" value="{{ profile.skills|join(', ') }}"><span class="field-help">Через запятую</span></label>
        </div>
        <div class="form-actions"><button class="btn btn-primary">Сохранить профиль</button></div>
      </form>
      <div class="panel-pad">
        <div class="panel-subtitle">Резюме этого профиля</div>
        <div class="compact-list">
          {% for item in resumes %}
          <div class="compact-row">
            <div>
              <strong>{{ item.name }}{% if item.is_default %} · основное{% endif %}</strong>
              <span>{{ item.category }} · {{ item.original_filename }}</span>
            </div>
            <div class="row-actions">
              {% if item.verified and item.active %}<span class="badge success">Подтверждено</span>
              {% elif item.verified and not item.active %}<span class="badge muted">Неактивно</span>
              {% else %}<span class="badge warning">Проверьте</span>{% endif %}
              {% if not item.storage_key.startswith('pending/') %}<a class="btn btn-sm" href="/admin/resumes/{{ item.id }}/file?profile_id={{ selected_profile_id }}" target="_blank" rel="noopener">Открыть</a>{% endif %}
              {% if not item.verified %}<form method="post" action="/admin/resumes/{{ item.id }}/verify"><input type="hidden" name="profile_id" value="{{ selected_profile_id }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-sm">Подтвердить</button></form>{% endif %}
              {% if item.active %}<form method="post" action="/admin/resumes/{{ item.id }}/deactivate" data-confirm-title="Деактивировать резюме?" data-confirm="Оно перестанет использоваться для новых откликов. Активировать можно обратно." data-confirm-action="Деактивировать" data-confirm-tone="danger"><input type="hidden" name="profile_id" value="{{ selected_profile_id }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-sm btn-danger">Деактивировать</button></form>
              {% else %}<form method="post" action="/admin/resumes/{{ item.id }}/activate"><input type="hidden" name="profile_id" value="{{ selected_profile_id }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-sm">Активировать</button></form>{% endif %}
              {% if not resume_usage.get(item.id) %}<form method="post" action="/admin/resumes/{{ item.id }}/delete" data-confirm-title="Удалить резюме?" data-confirm="Файл и запись будут удалены безвозвратно." data-confirm-action="Удалить" data-confirm-tone="danger"><input type="hidden" name="profile_id" value="{{ selected_profile_id }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="btn btn-sm btn-danger">Удалить</button></form>{% endif %}
            </div>
          </div>
          {% else %}<div class="empty-state compact">Резюме ещё не загружены.</div>{% endfor %}
        </div>
        <form class="compact-add" method="post" action="/admin/resumes" enctype="multipart/form-data">
          <input type="hidden" name="profile_id" value="{{ selected_profile_id }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <input type="text" name="name" placeholder="Название" required>
          <input type="text" name="category" placeholder="Категория" required>
          <input type="file" name="file" accept="application/pdf,.pdf" required>
          <label class="switch-inline"><input type="checkbox" name="make_default"> основное</label>
          <button class="btn btn-sm btn-primary">Загрузить</button>
        </form>
      </div>
    </div>
```

Then normalise the "Источники вакансий" block: keep its outer
`<details class="settings-section">`, and inside, render each source as a
`compact-row` with `row-actions` (toggle + "Обновить сейчас"), dropping any
inner disclosure. Do not change the source route actions.

Add minimal CSS for the new class names to `app/admin/templates/base.html`
`<style>` (follow the existing token/spacing style there):

```css
.row-actions{display:flex;flex-wrap:wrap;gap:.375rem;align-items:center}
.compact-add{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-top:.75rem}
.compact-add input[type=text]{flex:1 1 8rem;min-width:0}
.switch-inline{display:inline-flex;gap:.375rem;align-items:center;font-size:.875rem}
details.inline-create>summary{cursor:pointer;list-style:none;display:inline-flex}
details.inline-create>summary::-webkit-details-marker{display:none}
details.inline-create>.details-body{margin-top:.75rem}
```

- [ ] **Step 4: Run the render test to verify it passes**

Run:
```bash
uv run pytest tests/integration/test_interfaces.py -q -k "settings_page or admin_forms_merge or admin_login_mobile"
uv run ruff check .
uv run mypy app fixture_site
```
Expected: all pass. Fix any pre-existing settings-page assertion in
`test_admin_forms_merge_unexposed_fields_and_require_explicit_resume` that keys
off old markup (e.g. update selectors that referenced the removed
`Профиль кандидата` `<summary>` text — the daily-limit and preference assertions
are unaffected because that form is unchanged).

- [ ] **Step 5: Add the opt-in Playwright rendering check**

Add to `tests/integration/test_interfaces.py` (mirror the skip guard from
`tests/integration/test_phone_admin_review.py:510`):

```python
@pytest.mark.skipif(
    os.getenv("RUN_PLAYWRIGHT_TESTS") != "1" or importlib.util.find_spec("playwright") is None,
    reason="set RUN_PLAYWRIGHT_TESTS=1 and install Playwright to run the settings browser check",
)
@pytest.mark.asyncio
async def test_settings_page_playwright_narrow_view(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    from playwright import async_api as playwright_api

    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory, settings, suffix="settings-playwright"
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", follow_redirects=False
    ) as client:
        await _login_admin(client, settings)
        page_html = (
            await client.get(f"/?view=settings&profile_id={seeded['profile_id']}")
        ).text

    async with playwright_api.async_playwright() as runtime:
        browser = await runtime.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(page_html, wait_until="domcontentloaded")
        assert await page.get_by_text("Резюме этого профиля").count() == 1
        assert await page.locator("input[name='resume_file']").count() == 1
        assert await page.locator("form[action='/admin/resumes'] input[name='file']").count() == 1
        no_overflow = await page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        assert no_overflow is True
        await browser.close()
```

Add `import importlib.util` and `import os` at the top of the test module if not
already present.

- [ ] **Step 6: Run the Playwright check three consecutive times**

Run:
```bash
for run in 1 2 3; do
  RUN_PLAYWRIGHT_TESTS=1 uv run pytest tests/integration/test_interfaces.py -q -k settings_page_playwright || break
done
```
Expected: three consecutive passes (AGENTS.md browser gate). If Playwright
browsers are not installed, run `uv run playwright install chromium` first.

- [ ] **Step 7: Commit**

```bash
git add app/admin/templates/dashboard_settings.html app/admin/templates/base.html tests/integration/test_interfaces.py
git commit -m "feat: restructure settings into one profile block with a uniform list"
```

---

### Task 7: REST parity for resume delete / activate / deactivate

**Files:**
- Modify: `app/api/routes.py`
- Test: `tests/integration/test_interfaces.py`

**Interfaces:**
- Consumes: `ResumeService.delete`, `ResumeService.activate`,
  `ResumeService.deactivate`, `ResumeInUseError`, `safe_storage_path`,
  `UnsafeResumeError`, `record_audit_event`.
- Produces:
  - `DELETE /api/v1/resumes/{resume_id}` — bearer auth; `409` on `ResumeInUseError`;
    `404` on `LookupError`; else `{"id": <uuid>, "deleted": true}` and file
    unlinked after commit; audit `resume.deleted`.
  - `POST /api/v1/resumes/{resume_id}/activate` — `422` on `ValueError`; else
    `{"id": <uuid>, "active": true}`; audit `resume.activated`.
  - `POST /api/v1/resumes/{resume_id}/deactivate` — `{"id": <uuid>, "active": false}`;
    audit `resume.deactivated`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/integration/test_interfaces.py`:

```python
@pytest.mark.asyncio
async def test_rest_resume_delete_activate_deactivate(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="REST resume owner", is_default=True)
        session.add(profile)
        await session.flush()
        await session.commit()

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as client:
        upload = await client.post(
            "/api/v1/resumes",
            headers=headers,
            data={"name": "REST CV", "category": "ops"},
            files={"file": ("rest.pdf", b"%PDF-1.7\nrest\n%%EOF", "application/pdf")},
        )
        assert upload.status_code == 200
        resume_id = upload.json()["id"]

        deactivated = await client.post(
            f"/api/v1/resumes/{resume_id}/deactivate", headers=headers
        )
        assert deactivated.status_code == 200
        assert deactivated.json()["active"] is False

        activated = await client.post(
            f"/api/v1/resumes/{resume_id}/activate", headers=headers
        )
        assert activated.status_code == 200
        assert activated.json()["active"] is True

        deleted = await client.delete(f"/api/v1/resumes/{resume_id}", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json() == {"id": resume_id, "deleted": True}

    async with sqlite_session_factory() as session:
        assert await session.get(Resume, UUID(resume_id)) is None
        actions = set((await session.scalars(select(AuditEvent.action))).all())
        assert {"resume.deactivated", "resume.activated", "resume.deleted"} <= actions


@pytest.mark.asyncio
async def test_rest_resume_delete_conflicts_when_referenced(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory, settings, suffix="rest-delete-conflict"
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as client:
        response = await client.delete(
            f"/api/v1/resumes/{seeded['resume_id']}",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status_code == 409
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_interfaces.py -q -k rest_resume`
Expected: FAIL (`405`/`404` — endpoints not registered).

- [ ] **Step 3: Implement the endpoints**

In `app/api/routes.py`, add imports as needed
(`from app.profiles.service import ResumeInUseError`,
`from app.security.files import UnsafeResumeError, safe_storage_path`) and, after
`upload_resume`:

```python
@router.post("/resumes/{resume_id}/deactivate")
async def deactivate_resume_endpoint(
    resume_id: UUID,
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    item = await ResumeService(get_settings()).deactivate(session, resume_id)
    await record_audit_event(
        session,
        actor=actor,
        action="resume.deactivated",
        entity_type="resume",
        entity_id=str(item.id),
        correlation_id=str(item.id),
    )
    await session.commit()
    return {"id": item.id, "active": item.active}


@router.post("/resumes/{resume_id}/activate")
async def activate_resume_endpoint(
    resume_id: UUID,
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        item = await ResumeService(get_settings()).activate(session, resume_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await record_audit_event(
        session,
        actor=actor,
        action="resume.activated",
        entity_type="resume",
        entity_id=str(item.id),
        correlation_id=str(item.id),
    )
    await session.commit()
    return {"id": item.id, "active": item.active}


@router.delete("/resumes/{resume_id}")
async def delete_resume_endpoint(
    resume_id: UUID,
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    settings = get_settings()
    try:
        unlink_key = await ResumeService(settings).delete(session, resume_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResumeInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await record_audit_event(
        session,
        actor=actor,
        action="resume.deleted",
        entity_type="resume",
        entity_id=str(resume_id),
        correlation_id=str(resume_id),
    )
    await session.commit()
    if unlink_key is not None:
        try:
            safe_storage_path(settings.resume_storage_path, unlink_key).unlink(missing_ok=True)
        except UnsafeResumeError:
            pass
    return {"id": resume_id, "deleted": True}
```

Confirm `HTTPException` and `ResumeService` are already imported in
`app/api/routes.py`; add them if not.

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
uv run pytest tests/integration/test_interfaces.py -q -k rest_resume
uv run ruff check app/api/routes.py
uv run mypy app fixture_site
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/api/routes.py tests/integration/test_interfaces.py
git commit -m "feat: add REST resume delete, activate and deactivate endpoints"
```

---

### Task 8: MCP `delete_resume` tool

**Files:**
- Modify: `app/mcp/server.py`
- Test: `tests/integration/test_interfaces.py`

**Interfaces:**
- Consumes: `ResumeService.delete`, `ResumeInUseError`, `safe_storage_path`,
  `UnsafeResumeError`, `_audit_write`.
- Produces: MCP tool `delete_resume(resume_id: str) -> dict[str, Any]` — raises
  `ValueError` (surfaced to the client) when the resume is referenced or missing;
  otherwise deletes the row, unlinks the file, audits `resume.deleted`, and
  returns `{"id": resume_id, "deleted": True}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/integration/test_interfaces.py`:

```python
@pytest.mark.asyncio
async def test_mcp_delete_resume_removes_unreferenced_and_guards_referenced(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp import server as mcp_server

    _application, settings = interface_app
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)

    seeded = await _seed_review_application(
        sqlite_session_factory, settings, suffix="mcp-delete-resume"
    )
    with pytest.raises(ValueError):
        await mcp_server.delete_resume(resume_id=str(seeded["resume_id"]))

    async with sqlite_session_factory() as session:
        profile = UserProfile(name="MCP resume owner")
        session.add(profile)
        await session.flush()
        settings.resume_storage_path.mkdir(parents=True, exist_ok=True)
        (settings.resume_storage_path / "mcp-orphan.pdf").write_bytes(b"%PDF-1.4\nmcp\n%%EOF")
        orphan = Resume(
            profile_id=profile.id,
            name="MCP orphan",
            category="ops",
            storage_key="mcp-orphan.pdf",
            original_filename="mcp.pdf",
            mime_type="application/pdf",
            sha256=hashlib.sha256(b"%PDF-1.4\nmcp\n%%EOF").hexdigest(),
            active=True,
            verified=False,
            is_default=False,
        )
        session.add(orphan)
        await session.commit()
        orphan_id = orphan.id

    result = await mcp_server.delete_resume(resume_id=str(orphan_id))
    assert result == {"id": str(orphan_id), "deleted": True}
    async with sqlite_session_factory() as session:
        assert await session.get(Resume, orphan_id) is None
    assert not (settings.resume_storage_path / "mcp-orphan.pdf").exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/integration/test_interfaces.py -q -k mcp_delete_resume`
Expected: FAIL — `AttributeError: module 'app.mcp.server' has no attribute 'delete_resume'`.

- [ ] **Step 3: Implement the tool**

In `app/mcp/server.py`, after `deactivate_resume`:

```python
@mcp.tool()
async def delete_resume(resume_id: str) -> dict[str, Any]:
    """Delete a resume that no application or evaluation uses; the file is removed too."""
    from app.database.session import async_session_factory
    from app.profiles.service import ResumeInUseError
    from app.security.files import UnsafeResumeError, safe_storage_path

    settings = get_settings()
    async with async_session_factory() as session:
        try:
            unlink_key = await ResumeService(settings).delete(session, UUID(resume_id))
        except (LookupError, ResumeInUseError) as exc:
            raise ValueError(str(exc)) from exc
        await _audit_write(session, "resume.deleted", "resume", resume_id)
        await session.commit()
    if unlink_key is not None:
        try:
            safe_storage_path(settings.resume_storage_path, unlink_key).unlink(missing_ok=True)
        except UnsafeResumeError:
            pass
    return {"id": resume_id, "deleted": True}
```

Match the local-import style already used by the neighbouring tools in this file.

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
uv run pytest tests/integration/test_interfaces.py -q -k mcp_delete_resume
uv run ruff check app/mcp/server.py
uv run mypy app fixture_site
```
Expected: all pass.

- [ ] **Step 5: Verify the tool is exposed over streamable HTTP**

Run: `uv run pytest tests/integration/test_interfaces.py -q -k mcp_streamable_http`
Expected: still passes (the existing `tools/list` assertion tolerates a new tool;
if it asserts an exact tool set, add `delete_resume` to that expected set).

- [ ] **Step 6: Commit**

```bash
git add app/mcp/server.py tests/integration/test_interfaces.py
git commit -m "feat: add delete_resume MCP tool with reference guard"
```

---

### Task 9: Documentation and full verification sweep

**Files:**
- Modify: `README.md`
- Modify: `docs/security.md`

**Interfaces:**
- Consumes: everything from Tasks 1-8.
- Produces: updated operator documentation; a clean full-suite run.

- [ ] **Step 1: Update `README.md`**

In the "Профиль, резюме и пожелания" section, add:

- Контактный email и телефон профиля теперь попадают в подпись отправляемого
  письма (строки `Тел.:` и `Email:` на языке письма). Пустые поля строк не
  добавляют. Заявки, уже стоящие в очереди «Требуют решения», сохраняют прежнюю
  подпись до следующей полной переподготовки.
- Резюме в панели: `Открыть` (просмотр PDF в новой вкладке),
  `Деактивировать`/`Активировать`, `Удалить` (только если резюме не использовано
  ни в одном отклике или оценке). Те же действия есть в REST
  (`DELETE /api/v1/resumes/{id}`, `POST /api/v1/resumes/{id}/activate|deactivate`)
  и MCP (`delete_resume`, `activate_resume`, `deactivate_resume`).
- Новый профиль можно создать сразу с первым резюме одной формой в разделе
  «Профиль».

- [ ] **Step 2: Update `docs/security.md`**

Add a short paragraph: the authenticated admin session may open its own uploaded
resume PDF (`GET /admin/resumes/{id}/file`). This is not a regression of the
"no resume paths or contents" rule, which constrains the lower-trust MCP bearer
interface, not the operator's own browser session. `read_verified_resume` keeps
its path-safety, `O_NOFOLLOW`, size, `%PDF-` and sha256 checks on this path.

- [ ] **Step 3: Run the full verification sweep**

Run:
```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app fixture_site
uv run pytest
uv run alembic upgrade head
uv run alembic check
docker compose config --quiet
```
Expected: `ruff check`, `mypy`, `pytest` all green; `alembic check` reports
"No new upgrade operations detected." (no migration added); `docker compose
config` exits 0. If `ruff format --check .` flags only the six pre-existing
unformatted Phase 1/2 design Markdown files noted in the phone Phase 2b
acceptance doc, that is the known baseline — do not reformat unrelated docs.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/security.md
git commit -m "docs: document profile signature and resume lifecycle changes"
```

- [ ] **Step 5: Run the opt-in browser gate three times**

Run:
```bash
for run in 1 2 3; do
  RUN_PLAYWRIGHT_TESTS=1 uv run pytest tests/integration/test_interfaces.py -q -k "settings_page_playwright" || break
done
```
Expected: three consecutive passes. Record the result for the hand-off report.

---

## Self-Review

### Spec coverage

| Spec section | Task |
|---|---|
| §3 / §4.1 contacts in letter signature (ru/ro/en, empty→byte-identical, not a claim) | 1 |
| §4.2 `ResumeService.delete` + `ResumeInUseError`, post-commit unlink | 2 |
| §3 / §4.3 `GET /admin/resumes/{id}/file` inline PDF, placeholder/foreign 404, unauth redirect | 3 |
| §4.3 deactivate / activate / delete admin routes, 409, 422, audit labels, feedback notices | 4 |
| §4.4 `resume_usage` context (one query per table, no N+1) | 4 |
| §4.3 combined profile + first resume (`POST /admin/profiles`) | 5 |
| §4.6 single "Профиль" block, uniform list pattern, no nested `<details>`, field help text | 6 |
| §4.6 source list normalised to the same row markup | 6 (Step 3) |
| §9 Playwright browser gate, 3 consecutive runs | 6, 9 |
| §4.5 REST parity `DELETE` / `activate` / `deactivate` | 7 |
| §4.5 MCP `delete_resume` | 8 |
| §5 no migration; `alembic check` clean | 9 (Step 3) |
| §7 security: admin-only view rationale, `read_verified_resume` checks retained | 3, 9 |
| §2.2 / §8 in-flight letters keep old signature — documented, not force-migrated | 9 (Step 1) |
| §9 full sweep (`ruff`, `mypy`, `pytest`, `alembic`, `docker compose config`) | 9 (Step 3) |

Open questions from spec §10 (language `level`, E.164 normalization, collapsed
add-row) are deliberately out of scope and not implemented.

### Placeholder scan

No `TBD` / `TODO` / "handle edge cases" / "similar to Task N". Every code step
contains the literal code. Test steps contain full test bodies. The one
conditional instruction — "if that assertion keys off old markup, update it"
(Task 6 Step 4, Task 8 Step 5) — names the exact test and the exact reason.

### Type consistency

- `ResumeService.delete(session, resume_id) -> str | None` — defined in Task 2,
  consumed identically in Tasks 4, 7, 8 (each treats the return as
  `unlink_key: str | None` and unlinks when not `None`).
- `ResumeInUseError` — defined in `app/profiles/service.py` (Task 2), imported in
  Tasks 4 (`app/admin/routes.py`), 7 (`app/api/routes.py`), 8 (`app/mcp/server.py`).
- `_owned_resume(session, resume_id, profile_id) -> tuple[Resume, UserProfile]` —
  defined and used only within Task 4.
- `_letter_signature(profile, language) -> str` — defined and used only within
  Task 1.
- Route notice keys (`resume_deactivated`, `resume_activated`, `resume_deleted`,
  `profile_and_resume_created`) added to `_FEEDBACK_NOTICES` in Task 4 Step 3 and
  emitted by redirects in Tasks 4 and 5 — names match.
- `resume_usage` context key (Task 4 Step 4) consumed as `resume_usage.get(item.id)`
  in the template (Task 6 Step 3) — name matches.
