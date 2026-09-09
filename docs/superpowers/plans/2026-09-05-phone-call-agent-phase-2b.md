# Phone Call Agent Phase 2b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add evidence audio clips, a post-call LLM summary with a minimal Telegram notification, and the top-level `Звонки` admin section to the Phase 2a phone call agent.

**Architecture:** The live `CallOrchestrator` (in the `job-agent phone-agent` process) captures short GSM-downlink WAV clips mid-call, best-effort and non-blocking. A closed auto-answered session is marked `summary_state='pending'`; a Celery beat task (`finalize-pending-calls`, every 120 s) links the clips to their turns, summarizes the transcript through a new `PhoneSummaryProvider` over the local llmRouter (`prefer=quality`), and best-effort notifies over Telegram. A second beat task prunes clips by age and total size. A new `?view=calls` admin section (Live / История / Evidence) and extended `GET /api/v1/phone/sessions[/{id}]` surface it.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async, Alembic, FastAPI, Starlette/Jinja admin, Celery + Redis, httpx, pydantic v2, pytest / pytest-asyncio / httpx.MockTransport, selectolax (unrelated), structlog.

**Spec:** `docs/superpowers/specs/2026-09-05-phone-call-agent-phase-2b-design.md`

## Global Constraints

- `from __future__ import annotations` in every new module; strict `mypy` on `app/` (`mypy app fixture_site`); `ruff check .` and `ruff format --check .` clean; `pytest` output pristine under `filterwarnings=["error"]`.
- `PhoneGateClient` gains **only** `recent_call_audio` — never `dial` / `send_sms`.
- `app/observability/health.py::readiness_status()` is unchanged; phone/summary/evidence/Telegram degradation never affects `/ready`.
- Post-call work runs **only** in the Celery worker, never in the `job-agent phone-agent` process.
- The post-call model may write `communication_sessions.summary` and set `needs_review` **up only**; it may NOT write `CallFact` / `InterviewAppointment`, NOT normalize dates/times, NOT promote a fact to confirmed.
- Caller numbers masked everywhere new via `app.phone.numbers.mask_phone`. Employer transcript text is never logged raw (Phase 1 rule). `spoken_text` is assistant text and is safe to store/show.
- Telegram: fixed host `https://api.telegram.org`; `telegram_bot_token` is a `SecretStr`, added to the `empty_secret_is_unset` field-validator list, never logged. Messages carry no candidate `confirmed_facts` and no raw caller number. A delivery failure never changes call / session / `summary_state`.
- Evidence audio stays under `storage/phone_evidence/` (git-ignored via `storage/`) and the DB; retention bounded by age (`phone_evidence_retention_days`, default 30) and total size (`phone_evidence_max_total_mb`, default 500).
- No real llmRouter / Telegram / PhoneGate in CI — `httpx.MockTransport` + `FakePhoneGate` only. Real-call assertions are opt-in (`ENABLE_REALCALL_TESTS=true`), never CI.
- Tests must not read the operator's local `.env` (existing autouse conftest fixture).
- Migrations verified with `alembic upgrade head && alembic check` against DEV Postgres 16 (`RUN_SERVICE_INTEGRATION_TESTS=1`, `DATABASE_URL` on `127.0.0.1:55432`). Never touch PROD project `jobhunter` / `/srv/jobhunter-prod`; never restart `/srv/phonegate`.
- Commit prefix `feat:` / `fix:` / `test:` / `refactor:` / `docs:`; English commit messages; end every commit body with the two attribution trailers configured for this session.
- Alembic head at plan start: `e5f6a7b8c9d0` (phone phase 2a). The new migration's `down_revision` is `e5f6a7b8c9d0`.
- Shipping defaults: `phone_summary_llm_enabled=false`, `telegram_enabled=false`, `phone_evidence_max_clips_per_call=3`.

---

## File Structure

**New files:**
- `app/phone/evidence.py` — `EvidenceCapturer` (mid-call heuristic + fetch + atomic write), `link_session_evidence(session, db)`, `prune_phone_evidence()`.
- `app/phone/summary.py` — `CallSummary`, `CallSummaryContext`, `PhoneSummaryProvider`, `PhoneSummaryUnavailable`, `build_summary_context(...)`, `finalize_pending_calls()`.
- `app/phone/telegram.py` — `send_telegram_message(...)`, `TelegramDeliveryError`, `render_call_notification(summary, session, base_url)`.
- `app/admin/phone_routes.py` — relocated `_phone_health`, `phone_auto_answer_toggle`, `phone_call_action`; new `build_calls_context(...)`, `stream_evidence_clip(...)`.
- `app/admin/templates/calls.html` — the `?view=calls` page shell + tab bar.
- `app/admin/templates/_calls_live.html`, `_calls_history.html`, `_calls_history_detail.html`, `_calls_evidence.html` — tab partials.
- `migrations/versions/<rev>_phone_phase_2b.py` — the additive migration.
- Test files: `tests/unit/test_phone_evidence.py`, `tests/unit/test_phone_summary.py`, `tests/unit/test_phone_telegram.py`, `tests/unit/test_phone_finalize.py`, `tests/unit/test_phone_admin_calls.py`, `tests/integration/test_phone_evidence_e2e.py`.

**Modified files:**
- `app/models/enums.py` — add `PhoneSummaryState`.
- `app/models/entities.py` — `communication_sessions.summary`, `.summary_state`; `communication_turns.audio_evidence_path`.
- `app/phone/client.py` — `recent_call_audio(seconds)`.
- `app/phone/sessions.py` — `close()` marks `summary_state='pending'` for auto-answered; `set_turn_evidence_path`, `set_summary`.
- `app/phone/orchestrator.py` — construct `EvidenceCapturer` in `run()`, call `maybe_capture` in the `LISTENING` loop.
- `app/settings/config.py` — new `phone_evidence_*`, `phone_summary_llm_*`, `telegram_*` fields; secret-repr list; production validation.
- `app/scheduler/tasks.py` — `finalize_pending_calls_task`, `prune_phone_evidence_task`.
- `app/scheduler/celery_app.py` — beat entries + `task_routes` + `phone` queue.
- `docker-compose.yml`, `docker-compose.prod.yml` — add `phone` to the worker `-Q` list.
- `app/admin/routes.py` — move phone handlers out; add `"calls": "Звонки"` to `_VIEW_TITLES`; `elif view == "calls"` branch; `counts["phone_review"]`.
- `app/admin/__init__.py` — include the new phone admin router.
- `app/admin/templates/dashboard.html` — `Звонки` nav link + badge; `{% elif view == 'calls' %}` include.
- `app/api/phone_routes.py` — extend `/sessions` list + detail with 2b fields and filters.
- `tests/fixtures/fake_phonegate.py` — `GET /api/call/audio`, `set_call_audio`, `fail_next_audio`.
- `tests/unit/test_phone_migration.py`, `tests/integration/test_sqlite_migrations.py` — head bump + new column/enum assertions.
- `tests/unit/test_phone_client.py`, `tests/unit/test_phone_sessions.py`, `tests/unit/test_phone_settings.py`, `tests/unit/test_phone_api.py` — extend for the new surface.
- `.env.example` — commented placeholders for the new settings.
- `docs/security.md`, `docs/threat-model.md` — a short note on the Telegram outbound channel.

---

## Task 1: Schema — `PhoneSummaryState`, entity columns, migration

**Files:**
- Modify: `app/models/enums.py`
- Modify: `app/models/entities.py:614-663` (`CommunicationSession`), `:666-696` (`CommunicationTurn`)
- Create: `migrations/versions/<rev>_phone_phase_2b.py`
- Modify: `tests/unit/test_phone_migration.py`, `tests/integration/test_sqlite_migrations.py`
- Test: `tests/unit/test_phone_entities.py` (extend)

**Interfaces:**
- Produces: `PhoneSummaryState(StrEnum)` with `NOT_APPLICABLE="not_applicable"`, `PENDING="pending"`, `DONE="done"`, `FAILED="failed"`, `SKIPPED="skipped"`. `CommunicationSession.summary: Mapped[dict[str, Any]]` (JSON, default `dict`, non-null), `CommunicationSession.summary_state: Mapped[PhoneSummaryState]` (default `NOT_APPLICABLE`, non-null), `CommunicationTurn.audio_evidence_path: Mapped[str | None]` (String(255)).

- [ ] **Step 1: Write the failing entity test**

In `tests/unit/test_phone_entities.py` add:

```python
async def test_communication_session_has_summary_columns(async_session):
    from app.models.enums import PhoneSummaryState

    profile = await _make_profile(async_session)
    call = CommunicationSession(
        profile_id=profile.id,
        channel=CommunicationChannel.CALL,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        phonegate_event_id_start=1,
        started_at=utcnow(),
    )
    async_session.add(call)
    await async_session.flush()
    assert call.summary == {}
    assert call.summary_state is PhoneSummaryState.NOT_APPLICABLE


async def test_communication_turn_has_audio_evidence_path(
    async_session,
): ...  # create a session + turn, assert turn.audio_evidence_path is None
```

(Reuse the helpers already in `test_phone_entities.py`; match its existing fixture names.)

- [ ] **Step 2: Run it — expect failure**

Run: `uv run pytest tests/unit/test_phone_entities.py -q`
Expected: FAIL — `AttributeError: ... 'summary'` / `PhoneSummaryState` import error.

- [ ] **Step 3: Add the enum**

In `app/models/enums.py`, after `class TurnDeliveryStatus`:

```python
class PhoneSummaryState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
```

- [ ] **Step 4: Add the entity columns**

`app/models/entities.py` — import `PhoneSummaryState`; in `CommunicationSession` after `script_stage`:

```python
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    summary_state: Mapped[PhoneSummaryState] = mapped_column(
        enum_column(PhoneSummaryState),
        default=PhoneSummaryState.NOT_APPLICABLE,
        nullable=False,
    )
```

In `CommunicationTurn` after `spoken_text`:

```python
    audio_evidence_path: Mapped[str | None] = mapped_column(String(255))
```

- [ ] **Step 5: Run the entity test — expect pass**

Run: `uv run pytest tests/unit/test_phone_entities.py -q`
Expected: PASS.

- [ ] **Step 6: Write the migration**

`uv run alembic revision -m "phone phase 2b: call summary and audio evidence"` then replace the body:

```python
"""phone phase 2b: call summary and audio evidence

Revision ID: <rev>
Revises: e5f6a7b8c9d0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "<rev>"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUMMARY_STATE = sa.Enum(
    "not_applicable",
    "pending",
    "done",
    "failed",
    "skipped",
    name="phonesummarystate",
    native_enum=False,
)


def upgrade() -> None:
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.add_column(sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"))
        batch_op.add_column(
            sa.Column(
                "summary_state",
                _SUMMARY_STATE,
                nullable=False,
                server_default="not_applicable",
            )
        )
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.alter_column("summary", server_default=None)
        batch_op.alter_column("summary_state", server_default=None)
    with op.batch_alter_table("communication_turns") as batch_op:
        batch_op.add_column(sa.Column("audio_evidence_path", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("communication_turns") as batch_op:
        batch_op.drop_column("audio_evidence_path")
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.drop_column("summary_state")
        batch_op.drop_column("summary")
```

- [ ] **Step 7: Bump the migration-head tests**

In `tests/unit/test_phone_migration.py` and `tests/integration/test_sqlite_migrations.py`: update the expected head revision to `<rev>`; extend the column checks to assert `communication_sessions.summary`, `communication_sessions.summary_state`, `communication_turns.audio_evidence_path` exist after `upgrade head`. Match each file's existing assertion style.

- [ ] **Step 8: Run migration tests + alembic check**

Run: `uv run pytest tests/unit/test_phone_migration.py tests/integration/test_sqlite_migrations.py -q`
Then: `uv run alembic upgrade head && uv run alembic check`
Expected: PASS; `alembic check` reports "No new upgrade operations detected."

- [ ] **Step 9: Full type + lint + focused tests**

Run: `uv run ruff check app tests && uv run mypy app && uv run pytest tests/unit/test_phone_entities.py tests/unit/test_phone_enums.py -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add app/models/enums.py app/models/entities.py migrations/versions/ tests/unit/test_phone_entities.py tests/unit/test_phone_migration.py tests/integration/test_sqlite_migrations.py
git commit -m "feat: phone phase 2b schema — call summary and audio evidence columns"
```

---

## Task 2: `PhoneGateClient.recent_call_audio` + `FakePhoneGate` audio route

**Files:**
- Modify: `app/phone/client.py:32-145`
- Modify: `tests/fixtures/fake_phonegate.py`
- Test: `tests/unit/test_phone_client.py`, `tests/unit/test_fake_phonegate.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PhoneGateClient.recent_call_audio(self, seconds: int) -> bytes` — returns raw `audio/wav` bytes; raises `PhoneGateError` on 409 / 4xx, `PhoneGateUnavailable` on 5xx / transport error. `FakePhoneGate.set_call_audio(wav_bytes: bytes) -> None`, `FakePhoneGate.fail_next_audio() -> None`.

- [ ] **Step 1: Write the failing client test**

`tests/unit/test_phone_client.py`:

```python
@pytest.mark.asyncio
async def test_recent_call_audio_returns_wav_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/call/audio"
        assert request.url.params["seconds"] == "8"
        return httpx.Response(200, content=b"RIFFfake", headers={"content-type": "audio/wav"})

    client = PhoneGateClient(
        base_url="http://pg", token="t", transport=httpx.MockTransport(handler)
    )
    assert await client.recent_call_audio(8) == b"RIFFfake"
    await client.aclose()


@pytest.mark.asyncio
async def test_recent_call_audio_409_raises_phonegate_error():
    client = PhoneGateClient(
        base_url="http://pg",
        token="t",
        transport=httpx.MockTransport(lambda r: httpx.Response(409, json={"success": False})),
    )
    with pytest.raises(PhoneGateError):
        await client.recent_call_audio(5)
    await client.aclose()


@pytest.mark.asyncio
async def test_recent_call_audio_clamps_seconds():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["seconds"] = request.url.params["seconds"]
        return httpx.Response(200, content=b"x")

    client = PhoneGateClient(
        base_url="http://pg", token="t", transport=httpx.MockTransport(handler)
    )
    await client.recent_call_audio(99)
    assert seen["seconds"] == "10"
    await client.aclose()
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_client.py -k recent_call_audio -q`
Expected: FAIL — `AttributeError: 'PhoneGateClient' object has no attribute 'recent_call_audio'`.

- [ ] **Step 3: Implement `recent_call_audio`**

`app/phone/client.py`, add after `transcript(...)`:

```python
    async def recent_call_audio(self, seconds: int) -> bytes:
        clamped = max(1, min(int(seconds), 10))
        try:
            response = await self._client.get("/api/call/audio", params={"seconds": clamped})
        except httpx.RequestError as exc:
            raise PhoneGateUnavailable(f"/api/call/audio: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise PhoneGateUnavailable(f"/api/call/audio: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise PhoneGateError(f"/api/call/audio: HTTP {response.status_code}")
        return response.content
```

- [ ] **Step 4: Run client test — expect pass**

Run: `uv run pytest tests/unit/test_phone_client.py -k recent_call_audio -q`
Expected: PASS.

- [ ] **Step 5: Write the failing FakePhoneGate test**

`tests/unit/test_fake_phonegate.py`:

```python
@pytest.mark.asyncio
async def test_fake_phonegate_serves_recent_call_audio():
    fake = FakePhoneGate()
    fake.ring("+37360000000")
    fake.answer()
    fake.set_call_audio(b"RIFF....WAVEdata")
    client = PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport())
    assert await client.recent_call_audio(5) == b"RIFF....WAVEdata"
    fake.fail_next_audio()
    with pytest.raises(PhoneGateError):
        await client.recent_call_audio(5)
    await client.aclose()
```

- [ ] **Step 6: Run — expect failure**

Run: `uv run pytest tests/unit/test_fake_phonegate.py -k recent_call_audio -q`
Expected: FAIL — 404 from the fake (no route) → `PhoneGateError`, but `set_call_audio` is undefined first.

- [ ] **Step 7: Add the route + helpers to `FakePhoneGate`**

In `__init__`: `self._call_audio: bytes | None = None`, `self._fail_next_audio = False`; add `Route("/api/call/audio", self._audio_route)` to the routes list. Add:

```python
def set_call_audio(self, wav_bytes: bytes) -> None:
    self._call_audio = wav_bytes


def fail_next_audio(self) -> None:
    self._fail_next_audio = True


async def _audio_route(self, request: Request) -> Response:
    if not self._auth_ok(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    if self._fail_next_audio:
        self._fail_next_audio = False
        return JSONResponse({"success": False, "message": "нет аудио"}, status_code=409)
    if not self._call_audio:
        return JSONResponse({"success": False}, status_code=409)
    return Response(self._call_audio, media_type="audio/wav")
```

(Import `Response` from `starlette.responses`.)

- [ ] **Step 8: Run FakePhoneGate test + full phone-client + fake suites**

Run: `uv run pytest tests/unit/test_phone_client.py tests/unit/test_fake_phonegate.py -q`
Expected: PASS.

- [ ] **Step 9: Lint + type**

Run: `uv run ruff check app tests && uv run mypy app`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add app/phone/client.py tests/fixtures/fake_phonegate.py tests/unit/test_phone_client.py tests/unit/test_fake_phonegate.py
git commit -m "feat: PhoneGateClient.recent_call_audio + FakePhoneGate audio route"
```

---

## Task 3: `EvidenceCapturer` — heuristic + mid-call capture

**Files:**
- Create: `app/phone/evidence.py`
- Modify: `app/settings/config.py` (evidence settings only — full settings task is Task 8; add just what this task needs)
- Test: `tests/unit/test_phone_evidence.py`

**Interfaces:**
- Consumes: `PhoneGateClient.recent_call_audio` (Task 2); `app.phone.schemas.TranscriptEntry` (fields: `id: int`, `speaker: str`, `text: str`).
- Produces:
  - `is_important_utterance(text: str, *, min_chars: int) -> bool`
  - `class EvidenceCapturer` with `__init__(self, *, client: PhoneGateClient, settings: Settings, session_id: UUID)` and `async maybe_capture(self, rx_entries: list[TranscriptEntry]) -> None` (never raises). Writes `<phone_evidence_dir>/<session_id>/<entry.id>.wav`.
  - module constant `_KEYWORD_RE: re.Pattern[str]`.

- [ ] **Step 1: Add the evidence settings this task needs**

`app/settings/config.py`, in the phone block:

```python
    phone_evidence_dir: Path = Path("storage/phone_evidence")
    phone_evidence_seconds: int = Field(default=8, ge=1, le=10)
    phone_evidence_min_chars: int = Field(default=60, ge=10, le=500)
    phone_evidence_max_clips_per_call: int = Field(default=3, ge=0, le=20)
    phone_evidence_max_clip_bytes: int = Field(default=2_097_152, ge=65_536, le=8_388_608)
```

- [ ] **Step 2: Write failing heuristic tests**

`tests/unit/test_phone_evidence.py`:

```python
import pytest
from app.phone.evidence import is_important_utterance


@pytest.mark.parametrize(
    "text",
    [
        "В четверг в четырнадцать ноль ноль",  # digits via words + weekday
        "приходите на собеседование завтра",  # interview keyword
        "адрес улица Индустриальная 12",  # address + digit
        "офис на пятом этаже, кабинет 5",  # digit
        "a" * 61,  # length
        "vineri la ora zece",  # RO time
    ],
)
def test_is_important_true(text):
    assert is_important_utterance(text, min_chars=60) is True


@pytest.mark.parametrize("text", ["да", "хорошо, спасибо", "алло вы меня слышите"])
def test_is_important_false(text):
    assert is_important_utterance(text, min_chars=60) is False
```

- [ ] **Step 3: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_evidence.py -q`
Expected: FAIL — `ModuleNotFoundError: app.phone.evidence`.

- [ ] **Step 4: Implement the heuristic**

`app/phone/evidence.py`:

```python
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from uuid import UUID

import structlog

from app.phone.client import PhoneGateClient, PhoneGateError, PhoneGateUnavailable
from app.phone.schemas import TranscriptEntry
from app.settings.config import Settings

logger = structlog.get_logger(__name__)

_KEYWORD_RE = re.compile(
    r"понедельник|вторник|сред[ауы]|четверг|пятниц|суббот|воскресень"
    r"|\bпн\b|\bвт\b|\bср\b|\bчт\b|\bпт\b|\bсб\b|\bвс\b"
    r"|luni|mar[țt]i|miercuri|joi|vineri|s[âa]mb[ăa]t|duminic"
    r"|час[а-я]*|минут|утра|вечера|полудн|полдень|\bora\b|diminea[țt]|seara"
    r"|улиц|\bул\.?\b|бульвар|проспект|\bstr\.?\b|adresa|sector|офис|кабинет|\bкаб\.?\b"
    r"|собеседовани|интервью|встреч|резюме|\bcv\b|документ|приход|жд[её]м|interviu",
    re.IGNORECASE,
)
_DIGIT_RE = re.compile(r"\d")


def is_important_utterance(text: str, *, min_chars: int) -> bool:
    stripped = text.strip()
    if len(stripped) >= min_chars:
        return True
    if _DIGIT_RE.search(stripped):
        return True
    return _KEYWORD_RE.search(stripped) is not None
```

- [ ] **Step 5: Run heuristic tests — expect pass**

Run: `uv run pytest tests/unit/test_phone_evidence.py -q`
Expected: PASS.

- [ ] **Step 6: Write failing `EvidenceCapturer` tests**

Append to `tests/unit/test_phone_evidence.py`:

```python
from uuid import uuid4
from app.phone.evidence import EvidenceCapturer
from app.phone.schemas import TranscriptEntry


class _FakeClient:
    def __init__(self, audio=b"RIFFwav", exc=None):
        self.audio, self.exc, self.calls = audio, exc, 0

    async def recent_call_audio(self, seconds):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.audio


def _entry(i, text, speaker="rx"):
    return TranscriptEntry.model_validate(
        {"id": i, "speaker": speaker, "text": text, "timestamp_ms": 0}
    )


@pytest.mark.asyncio
async def test_capturer_writes_clip_for_important_line(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={"phone_evidence_dir": tmp_path})
    sid = uuid4()
    client = _FakeClient()
    cap = EvidenceCapturer(client=client, settings=settings, session_id=sid)
    await cap.maybe_capture([_entry(7, "в четверг в 14:00 приходите")])
    assert (tmp_path / str(sid) / "7.wav").read_bytes() == b"RIFFwav"


@pytest.mark.asyncio
async def test_capturer_ignores_unimportant_and_tx(tmp_path):
    settings = get_settings().model_copy(update={"phone_evidence_dir": tmp_path})
    cap = EvidenceCapturer(client=_FakeClient(), settings=settings, session_id=uuid4())
    await cap.maybe_capture([_entry(1, "да"), _entry(2, "текст", speaker="tx")])
    assert not any(tmp_path.rglob("*.wav"))


@pytest.mark.asyncio
async def test_capturer_respects_per_call_cap(tmp_path):
    settings = get_settings().model_copy(
        update={"phone_evidence_dir": tmp_path, "phone_evidence_max_clips_per_call": 2}
    )
    cap = EvidenceCapturer(client=_FakeClient(), settings=settings, session_id=uuid4())
    await cap.maybe_capture([_entry(i, f"важная реплика номер {i}") for i in (1, 2, 3, 4)])
    assert len(list(tmp_path.rglob("*.wav"))) == 2


@pytest.mark.asyncio
async def test_capturer_swallows_fetch_error_without_consuming_cap(tmp_path):
    settings = get_settings().model_copy(
        update={"phone_evidence_dir": tmp_path, "phone_evidence_max_clips_per_call": 1}
    )
    failing = _FakeClient(exc=PhoneGateError("409"))
    cap = EvidenceCapturer(client=failing, settings=settings, session_id=uuid4())
    await cap.maybe_capture([_entry(1, "важная реплика раз")])
    assert not any(tmp_path.rglob("*.wav"))
    ok = _FakeClient()
    cap._client = ok  # same capturer, cap not yet consumed
    await cap.maybe_capture([_entry(2, "важная реплика два")])
    assert (tmp_path / cap._session_dir.name / "2.wav").exists()


@pytest.mark.asyncio
async def test_capturer_disabled_when_cap_zero(tmp_path):
    settings = get_settings().model_copy(
        update={"phone_evidence_dir": tmp_path, "phone_evidence_max_clips_per_call": 0}
    )
    cap = EvidenceCapturer(client=_FakeClient(), settings=settings, session_id=uuid4())
    await cap.maybe_capture([_entry(1, "важная реплика")])
    assert not any(tmp_path.rglob("*.wav"))
```

(Import `get_settings` from `app.settings`.)

- [ ] **Step 7: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_evidence.py -q`
Expected: FAIL — `ImportError: cannot import name 'EvidenceCapturer'`.

- [ ] **Step 8: Implement `EvidenceCapturer`**

Append to `app/phone/evidence.py`:

```python
class EvidenceCapturer:
    """Best-effort mid-call GSM-downlink clip capture. Never raises."""

    def __init__(self, *, client: PhoneGateClient, settings: Settings, session_id: UUID) -> None:
        self._client = client
        self._s = settings
        self._session_id = session_id
        self._session_dir = Path(settings.phone_evidence_dir) / str(session_id)
        self._captured = 0

    async def maybe_capture(self, rx_entries: list[TranscriptEntry]) -> None:
        cap = self._s.phone_evidence_max_clips_per_call
        if cap <= 0:
            return
        for entry in rx_entries:
            if self._captured >= cap:
                return
            if entry.speaker != "rx":
                continue
            if not is_important_utterance(entry.text, min_chars=self._s.phone_evidence_min_chars):
                continue
            if await self._capture_one(entry.id):
                self._captured += 1

    async def _capture_one(self, transcript_id: int) -> bool:
        try:
            wav = await self._client.recent_call_audio(self._s.phone_evidence_seconds)
        except (PhoneGateUnavailable, PhoneGateError) as exc:
            logger.warning("phone_evidence_capture_failed", reason=type(exc).__name__)
            return False
        if not wav or len(wav) > self._s.phone_evidence_max_clip_bytes:
            logger.warning("phone_evidence_capture_failed", reason="empty_or_oversized")
            return False
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            target = self._session_dir / f"{transcript_id}.wav"
            fd, tmp = tempfile.mkstemp(dir=self._session_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as handle:
                handle.write(wav)
            os.replace(tmp, target)
            return True
        except OSError as exc:
            logger.warning("phone_evidence_capture_failed", reason=type(exc).__name__)
            return False
```

- [ ] **Step 9: Run evidence tests + lint + type**

Run: `uv run pytest tests/unit/test_phone_evidence.py -q && uv run ruff check app tests && uv run mypy app`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add app/phone/evidence.py app/settings/config.py tests/unit/test_phone_evidence.py
git commit -m "feat: EvidenceCapturer — deterministic mid-call clip capture"
```

---

## Task 4: Wire `EvidenceCapturer` into `CallOrchestrator`; `SessionStore.close` marks pending

**Files:**
- Modify: `app/phone/orchestrator.py:61-74` (`__init__`), `:83-109` (`run`), `:188-225` (`LISTENING` loop)
- Modify: `app/phone/sessions.py:92-112` (`close`)
- Test: `tests/unit/test_phone_orchestrator.py`, `tests/unit/test_phone_orchestrator_loop.py`, `tests/unit/test_phone_sessions.py`

**Interfaces:**
- Consumes: `EvidenceCapturer` (Task 3), `PhoneSummaryState` (Task 1).
- Produces: `CallOrchestrator` constructs one `EvidenceCapturer` per call and calls `maybe_capture` on each new rx batch in `LISTENING`. `SessionStore.close(...)` sets `call.summary_state = PhoneSummaryState.PENDING` when `call.auto_answered` and the current state is `NOT_APPLICABLE`.

- [ ] **Step 1: Write failing `SessionStore.close` test**

`tests/unit/test_phone_sessions.py`:

```python
@pytest.mark.asyncio
async def test_close_marks_auto_answered_session_summary_pending(async_session):
    store = SessionStore()
    call = await _open_call(async_session, store)  # existing helper
    await store.mark_auto_answered(call, utcnow())
    await store.close(
        async_session, call, outcome=CommunicationOutcome.COMPLETED, ended_at=utcnow()
    )
    assert call.summary_state is PhoneSummaryState.PENDING


@pytest.mark.asyncio
async def test_close_leaves_non_auto_answered_summary_not_applicable(async_session):
    store = SessionStore()
    call = await _open_call(async_session, store)
    await store.close(async_session, call, outcome=CommunicationOutcome.MISSED, ended_at=utcnow())
    assert call.summary_state is PhoneSummaryState.NOT_APPLICABLE
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_sessions.py -k summary -q`
Expected: FAIL — `summary_state` stays `not_applicable` / attribute default.

- [ ] **Step 3: Implement in `SessionStore.close`**

`app/phone/sessions.py`, inside `close(...)` before `call.updated_at = utcnow()`:

```python
        if call.auto_answered and call.summary_state is PhoneSummaryState.NOT_APPLICABLE:
            call.summary_state = PhoneSummaryState.PENDING
```

(Import `PhoneSummaryState` from `app.models.enums`.)

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/unit/test_phone_sessions.py -k summary -q`
Expected: PASS.

- [ ] **Step 5: Write failing orchestrator capture test**

`tests/unit/test_phone_orchestrator_loop.py` (this file already drives the loop with a fake client + `FakePhoneGate`). Add a test: an auto-answered call, during `LISTENING` the fake emits an rx transcript line `"в четверг в 14:00 на Индустриальной 12"`, `fake.set_call_audio(b"RIFFclip")`; after the orchestrator finishes, assert `storage/phone_evidence/<session_id>/<transcript_id>.wav` exists with `b"RIFFclip"`. Use a `tmp_path` `phone_evidence_dir` via a settings override consistent with how this test file already injects settings.

- [ ] **Step 6: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_orchestrator_loop.py -k evidence -q`
Expected: FAIL — no clip file written.

- [ ] **Step 7: Wire the capturer**

`app/phone/orchestrator.py`:

- import: `from app.phone.evidence import EvidenceCapturer`
- in `run()`, right after `self._session_id = session_id`:

```python
self._evidence = EvidenceCapturer(client=self._client, settings=self._s, session_id=session_id)
```

- in the `LISTENING` `while True:` loop, replace the `if page.entries:` block so it captures on the rx subset:

```python
            if page.entries:
                seen_transcript_id = max(seen_transcript_id, max(e.id for e in page.entries))
                rx_entries = [e for e in page.entries if e.speaker == "rx"]
                if rx_entries:
                    last_activity = now
                    await self._evidence.maybe_capture(rx_entries)
```

(`maybe_capture` never raises, but keep it inside the existing `try` for the poll — it is already there.)

- [ ] **Step 8: Run orchestrator + loop suites**

Run: `uv run pytest tests/unit/test_phone_orchestrator.py tests/unit/test_phone_orchestrator_loop.py -q`
Expected: PASS (including the new evidence test; the pre-existing ones unaffected — `maybe_capture` on an empty/unimportant batch is a no-op).

- [ ] **Step 9: Lint + type + full phone suite**

Run: `uv run ruff check app tests && uv run mypy app && uv run pytest tests/unit -k phone -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add app/phone/orchestrator.py app/phone/sessions.py tests/unit/test_phone_orchestrator_loop.py tests/unit/test_phone_sessions.py
git commit -m "feat: capture evidence clips during LISTENING; mark auto-answered sessions summary-pending"
```

---

## Task 5: Settings — summary + Telegram fields, secret handling, production validation

**Files:**
- Modify: `app/settings/config.py`
- Modify: `.env.example`
- Test: `tests/unit/test_phone_settings.py`

**Interfaces:**
- Produces settings:
  `phone_evidence_retention_days: int = 30` (1–365), `phone_evidence_max_total_mb: int = 500` (10–10000),
  `phone_summary_llm_enabled: bool = False`, `phone_summary_llm_base_url: str = "http://127.0.0.1:4000"`,
  `phone_summary_llm_api_key: SecretStr | None = None`, `phone_summary_llm_model: str = ""`,
  `phone_summary_llm_prefer: Literal["fast","cheap","quality","balanced"] = "quality"`,
  `phone_summary_llm_timeout_seconds: float = 60.0` (10–300), `phone_summary_max_attempts: int = 3` (1–10),
  `phone_summary_batch: int = 10` (1–100),
  `telegram_enabled: bool = False`, `telegram_bot_token: SecretStr | None = None`, `telegram_chat_id: str | None = None`.
- Helper: `Settings.effective_summary_model -> str` (returns `phone_summary_llm_model or (openai_model or "")`).

- [ ] **Step 1: Write failing settings tests**

`tests/unit/test_phone_settings.py`:

```python
def test_summary_and_telegram_defaults():
    s = Settings(_env_file=None)
    assert s.phone_summary_llm_enabled is False
    assert s.telegram_enabled is False
    assert s.phone_summary_llm_prefer == "quality"
    assert s.phone_evidence_retention_days == 30
    assert s.effective_summary_model == ""


def test_summary_model_falls_back_to_openai_model():
    s = Settings(_env_file=None, openai_model="gpt-x")
    assert s.effective_summary_model == "gpt-x"
    s2 = Settings(_env_file=None, openai_model="gpt-x", phone_summary_llm_model="qwen")
    assert s2.effective_summary_model == "qwen"


def test_production_requires_telegram_creds_when_enabled():
    with pytest.raises(ValueError, match="TELEGRAM"):
        _production_settings(
            telegram_enabled=True
        )  # local helper mirroring the file's other prod tests


def test_production_requires_summary_model_when_enabled():
    with pytest.raises(ValueError, match="summary model"):
        _production_settings(phone_summary_llm_enabled=True, openai_model=None)


def test_empty_telegram_token_is_unset():
    assert Settings(_env_file=None, telegram_bot_token="  ").telegram_bot_token is None
```

(Follow the file's existing pattern for building a production `Settings` — copy its helper if there is one, else add a small `_production_settings(**over)` that supplies the mandatory prod fields.)

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_settings.py -q`
Expected: FAIL — unknown fields / no `effective_summary_model`.

- [ ] **Step 3: Add the fields**

`app/settings/config.py`, in the phone block after `phone_evidence_max_clip_bytes`:

```python
    phone_evidence_retention_days: int = Field(default=30, ge=1, le=365)
    phone_evidence_max_total_mb: int = Field(default=500, ge=10, le=10_000)

    phone_summary_llm_enabled: bool = False
    phone_summary_llm_base_url: str = "http://127.0.0.1:4000"
    phone_summary_llm_api_key: SecretStr | None = None
    phone_summary_llm_model: str = ""
    phone_summary_llm_prefer: Literal["fast", "cheap", "quality", "balanced"] = "quality"
    phone_summary_llm_timeout_seconds: float = Field(default=60.0, ge=10.0, le=300.0)
    phone_summary_max_attempts: int = Field(default=3, ge=1, le=10)
    phone_summary_batch: int = Field(default=10, ge=1, le=100)

    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
```

- [ ] **Step 4: Secret handling + helper + validation**

- Add `"phone_summary_llm_api_key"` and `"telegram_bot_token"` to the `empty_secret_is_unset` `@field_validator(...)` name list.
- Add the helper property:

```python
    @property
    def effective_summary_model(self) -> str:
        return self.phone_summary_llm_model.strip() or (self.openai_model or "").strip()
```

- In `validate_secure_production`, before `return self` (guard each with `self.environment == "production"` already handled by the early return):

```python
if self.telegram_enabled and (self.telegram_bot_token is None or not self.telegram_chat_id):
    raise ValueError(
        "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required when TELEGRAM_ENABLED is true"
    )
if self.phone_summary_llm_enabled and not self.effective_summary_model:
    raise ValueError("an explicit summary model is required when PHONE_SUMMARY_LLM_ENABLED is true")
if (
    self.phone_summary_llm_enabled
    and self.phone_summary_llm_api_key is None
    and self.llmrouter_api_key is None
):
    raise ValueError("a summary LLM API key is required when PHONE_SUMMARY_LLM_ENABLED is true")
```

- [ ] **Step 5: Run settings tests — expect pass**

Run: `uv run pytest tests/unit/test_phone_settings.py -q`
Expected: PASS.

- [ ] **Step 6: Update `.env.example`**

Append, commented:

```bash
# --- Phone: evidence clips (captured only for auto-answered calls) ---
# PHONE_EVIDENCE_MAX_CLIPS_PER_CALL=3   # 0 disables capture
# PHONE_EVIDENCE_RETENTION_DAYS=30
# PHONE_EVIDENCE_MAX_TOTAL_MB=500
# --- Phone: post-call summary (Celery; off by default) ---
# PHONE_SUMMARY_LLM_ENABLED=false
# PHONE_SUMMARY_LLM_BASE_URL=http://127.0.0.1:4000
# PHONE_SUMMARY_LLM_API_KEY=
# PHONE_SUMMARY_LLM_MODEL=            # falls back to OPENAI_MODEL when empty
# PHONE_SUMMARY_LLM_PREFER=quality
# --- Phone: Telegram post-call notification (off by default) ---
# TELEGRAM_ENABLED=false
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=
```

- [ ] **Step 7: Lint + type + full settings suite**

Run: `uv run ruff check app tests && uv run mypy app && uv run pytest tests/unit/test_phone_settings.py tests/unit/test_settings.py -q`
Expected: PASS (run whatever the repo's general settings test file is called if different).

- [ ] **Step 8: Commit**

```bash
git add app/settings/config.py .env.example tests/unit/test_phone_settings.py
git commit -m "feat: phone 2b settings — summary LLM, Telegram, evidence retention"
```

---

## Task 6: `CallSummary` schema + `PhoneSummaryProvider`

**Files:**
- Modify: `app/phone/summary.py` (create in this task)
- Test: `tests/unit/test_phone_summary.py`

**Interfaces:**
- Consumes: settings from Task 5.
- Produces:
  - `class CallSummary(BaseModel)`: `summary_text: str`, `mentioned_vacancy: str = ""`, `proposed_datetime_text: str = ""`, `proposed_address_text: str = ""`, `contact_person_text: str = ""`, `outcome_guess: Literal["interview_proposed","info_request","not_relevant","unclear","other"] = "unclear"`, `needs_review: bool = False`.
  - `class CallSummaryContext(BaseModel)`: `transcript: list[tuple[str, str]]` (speaker, text), `company: str | None`, `vacancy: str | None`, `application_status: str | None`, `confirmed_facts: dict[str, Any]`.
  - `class PhoneSummaryUnavailable(RuntimeError)`.
  - `class PhoneSummaryProvider` with `__init__(self, *, base_url: str, api_key: str, model: str, prefer: str = "quality", timeout_seconds: float = 60.0, client: httpx.AsyncClient | None = None)` and `async summarize(self, ctx: CallSummaryContext) -> CallSummary`.

- [ ] **Step 1: Write failing provider tests**

`tests/unit/test_phone_summary.py`:

```python
import httpx, json, pytest
from app.phone.summary import (
    CallSummary,
    CallSummaryContext,
    PhoneSummaryProvider,
    PhoneSummaryUnavailable,
)

_CTX = CallSummaryContext(
    transcript=[
        ("assistant", "Здравствуйте"),
        ("employer", "Звоню по вакансии грузчика, в четверг в 14"),
    ],
    company="Example SRL",
    vacancy="Грузчик",
    application_status="sent",
    confirmed_facts={},
)


def _ok_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]},
    )


@pytest.mark.asyncio
async def test_summarize_parses_valid_json():
    payload = {
        "summary_text": "Работодатель предложил собеседование в четверг в 14:00.",
        "mentioned_vacancy": "Грузчик",
        "proposed_datetime_text": "в четверг в 14",
        "proposed_address_text": "",
        "contact_person_text": "",
        "outcome_guess": "interview_proposed",
        "needs_review": False,
    }
    seen = {}

    def handler(request):
        seen["prefer"] = request.headers.get("X-LLMRouter-Prefer")
        seen["url"] = str(request.url)
        return _ok_response(payload)

    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await p.summarize(_CTX)
    assert result.outcome_guess == "interview_proposed"
    assert result.proposed_datetime_text == "в четверг в 14"
    assert seen["prefer"] == "quality"
    assert seen["url"].endswith("/v1/chat/completions")


@pytest.mark.asyncio
async def test_summarize_rejects_non_json():
    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"finish_reason": "stop", "message": {"content": "sorry I cannot"}}
                        ]
                    },
                )
            )
        ),
    )
    with pytest.raises(PhoneSummaryUnavailable):
        await p.summarize(_CTX)


@pytest.mark.asyncio
async def test_summarize_maps_5xx_and_timeout():
    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
    )
    with pytest.raises(PhoneSummaryUnavailable):
        await p.summarize(_CTX)


@pytest.mark.asyncio
async def test_summarize_strips_markdown_fence():
    fenced = "```json\n" + json.dumps({"summary_text": "ок"}) + "\n```"
    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={"choices": [{"finish_reason": "stop", "message": {"content": fenced}}]},
                )
            )
        ),
    )
    assert (await p.summarize(_CTX)).summary_text == "ок"
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_summary.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `app/phone/summary.py` (schema + provider only)**

```python
from __future__ import annotations

import json
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ValidationError

logger = structlog.get_logger(__name__)

_SYSTEM = (
    "You summarize a finished phone call between an employer and a job candidate's "
    "voice assistant. Reply with ONE JSON object and nothing else. Write summary_text "
    "in Russian, 2-4 sentences. Copy the caller's wording for dates, times and "
    "addresses into the *_text fields — do NOT normalize them. Never invent facts about "
    "the candidate. Set needs_review=true if the outcome, date, time or address is "
    "unclear or contradictory."
)


class CallSummary(BaseModel):
    summary_text: str
    mentioned_vacancy: str = ""
    proposed_datetime_text: str = ""
    proposed_address_text: str = ""
    contact_person_text: str = ""
    outcome_guess: Literal[
        "interview_proposed", "info_request", "not_relevant", "unclear", "other"
    ] = "unclear"
    needs_review: bool = False


class CallSummaryContext(BaseModel):
    transcript: list[tuple[str, str]]
    company: str | None = None
    vacancy: str | None = None
    application_status: str | None = None
    confirmed_facts: dict[str, Any] = {}


class PhoneSummaryUnavailable(RuntimeError):
    """The summary model did not return a usable result."""


def _strip_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().casefold() in {"```json", "```"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return text.strip()


class PhoneSummaryProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        prefer: str = "quality",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model.strip()
        self._prefer = prefer
        self._timeout = timeout_seconds
        self._client = client

    def _body(self, ctx: CallSummaryContext) -> dict[str, Any]:
        header = []
        if ctx.company:
            header.append(f"Компания: {ctx.company}")
        if ctx.vacancy:
            header.append(f"Вакансия: {ctx.vacancy}")
        if ctx.application_status:
            header.append(f"Статус отклика: {ctx.application_status}")
        lines = [f"{who}: {text}" for who, text in ctx.transcript]
        user = "\n".join([*header, "", "Транскрипт:", *lines])
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": 700,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "call_summary",
                    "strict": True,
                    "schema": CallSummary.model_json_schema(),
                },
            },
        }

    async def summarize(self, ctx: CallSummaryContext) -> CallSummary:
        if self._client is not None:
            return await self._summarize_with(self._client, ctx)
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
            return await self._summarize_with(client, ctx)

    async def _summarize_with(
        self, client: httpx.AsyncClient, ctx: CallSummaryContext
    ) -> CallSummary:
        headers = {"Authorization": f"Bearer {self._api_key}", "X-LLMRouter-Prefer": self._prefer}
        try:
            response = await client.post(
                f"{self._base_url}/v1/chat/completions", headers=headers, json=self._body(ctx)
            )
        except httpx.RequestError as exc:
            raise PhoneSummaryUnavailable(f"transport:{type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise PhoneSummaryUnavailable(f"http_{response.status_code}")
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise PhoneSummaryUnavailable("malformed_envelope") from exc
        if not isinstance(content, str) or not content.strip():
            raise PhoneSummaryUnavailable("empty_content")
        try:
            return CallSummary.model_validate_json(_strip_fence(content))
        except ValidationError as exc:
            raise PhoneSummaryUnavailable("schema_mismatch") from exc
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/unit/test_phone_summary.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + type**

Run: `uv run ruff check app tests && uv run mypy app`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/phone/summary.py tests/unit/test_phone_summary.py
git commit -m "feat: PhoneSummaryProvider — llmRouter-backed post-call summary schema"
```

---

## Task 7: Telegram sender

**Files:**
- Create: `app/phone/telegram.py`
- Modify: `docs/security.md`, `docs/threat-model.md`
- Test: `tests/unit/test_phone_telegram.py`

**Interfaces:**
- Consumes: `CallSummary` (Task 6).
- Produces:
  - `class TelegramDeliveryError(RuntimeError)`.
  - `async send_telegram_message(*, token: str, chat_id: str, text: str, timeout: float = 10.0, client: httpx.AsyncClient | None = None) -> None`.
  - `render_call_notification(summary: CallSummary, *, company: str | None, vacancy: str | None, session_id: str, base_url: str | None) -> str`.

- [ ] **Step 1: Write failing tests**

`tests/unit/test_phone_telegram.py`:

```python
import httpx, pytest
from app.phone.summary import CallSummary
from app.phone.telegram import (
    TelegramDeliveryError,
    render_call_notification,
    send_telegram_message,
)


@pytest.mark.asyncio
async def test_send_ok():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["json"] = request.read()
        return httpx.Response(200, json={"ok": True})

    await send_telegram_message(
        token="T",
        chat_id="42",
        text="hi",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert seen["url"] == "https://api.telegram.org/botT/sendMessage"


@pytest.mark.asyncio
async def test_send_non_2xx_raises():
    with pytest.raises(TelegramDeliveryError):
        await send_telegram_message(
            token="T",
            chat_id="42",
            text="hi",
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))),
        )


@pytest.mark.asyncio
async def test_send_network_error_raises():
    def boom(r):
        raise httpx.ConnectError("down")

    with pytest.raises(TelegramDeliveryError):
        await send_telegram_message(
            token="T",
            chat_id="42",
            text="hi",
            client=httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        )


def test_render_confident_and_uncertain_hide_caller_number():
    confident = CallSummary(
        summary_text="Собеседование в четверг.",
        outcome_guess="interview_proposed",
        proposed_datetime_text="четверг 14:00",
        proposed_address_text="ул. Индустриальная 12",
    )
    text = render_call_notification(
        confident,
        company="Example SRL",
        vacancy="Грузчик",
        session_id="abc",
        base_url="https://jobs.example.com",
    )
    assert "Example SRL" in text and "четверг 14:00" in text
    assert "https://jobs.example.com/?view=calls&session=abc" in text
    uncertain = CallSummary(summary_text="Неясно.", needs_review=True)
    u = render_call_notification(
        uncertain, company=None, vacancy=None, session_id="abc", base_url=None
    )
    assert "Требуется проверка" in u and "🔗" not in u


def test_render_escapes_html():
    s = CallSummary(summary_text="<b>x</b> & y")
    assert "&lt;b&gt;" in render_call_notification(
        s, company="A<b>", vacancy=None, session_id="s", base_url=None
    )
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_telegram.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `app/phone/telegram.py`**

```python
from __future__ import annotations

from html import escape

import httpx

from app.phone.summary import CallSummary

_API = "https://api.telegram.org"


class TelegramDeliveryError(RuntimeError):
    """Telegram did not accept the message."""


async def send_telegram_message(
    *,
    token: str,
    chat_id: str,
    text: str,
    timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    url = f"{_API}/bot{token}/sendMessage"
    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        response = await client.post(url, json=payload)
    except httpx.RequestError as exc:
        raise TelegramDeliveryError(f"transport:{type(exc).__name__}") from exc
    finally:
        if owns:
            await client.aclose()
    if response.status_code >= 300:
        raise TelegramDeliveryError(f"http_{response.status_code}")


def render_call_notification(
    summary: CallSummary,
    *,
    company: str | None,
    vacancy: str | None,
    session_id: str,
    base_url: str | None,
) -> str:
    link = f"\n🔗 {base_url}/?view=calls&session={session_id}" if base_url else ""
    body = escape(summary.summary_text)
    if summary.needs_review or summary.outcome_guess != "interview_proposed":
        head = escape(company) if company else "Неизвестная компания"
        return f"📞 {head}\n{body}\n⚠️ Требуется проверка — открой запись звонка.{link}"
    title = escape(company or "")
    if vacancy:
        title = f"{title} — {escape(vacancy)}" if title else escape(vacancy)
    parts = [f"📞 {title}".rstrip(), body]
    if summary.proposed_datetime_text:
        parts.append(f"🕒 {escape(summary.proposed_datetime_text)}")
    if summary.proposed_address_text:
        parts.append(f"📍 {escape(summary.proposed_address_text)}")
    return "\n".join(parts) + link
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/unit/test_phone_telegram.py -q`
Expected: PASS.

- [ ] **Step 5: Doc notes**

`docs/security.md` and `docs/threat-model.md` — add a short subsection: "Telegram post-call notification (Phase 2b)": outbound HTTPS to the fixed host `api.telegram.org` only; `TELEGRAM_BOT_TOKEN` stored as a `SecretStr`, never logged; the message contains company / vacancy / the model summary text and a panel link — no raw caller number, no candidate `confirmed_facts`; delivery is best-effort and off by default; a failure never affects call handling.

- [ ] **Step 6: Lint + type + commit**

```bash
uv run ruff check app tests && uv run mypy app && uv run pytest tests/unit/test_phone_telegram.py -q
git add app/phone/telegram.py docs/security.md docs/threat-model.md tests/unit/test_phone_telegram.py
git commit -m "feat: best-effort Telegram post-call notification"
```

---

## Task 8: `finalize_pending_calls` — evidence linking + summary + notify

**Files:**
- Modify: `app/phone/summary.py` (add `build_summary_context`, `finalize_pending_calls`)
- Modify: `app/phone/evidence.py` (add `link_session_evidence`)
- Modify: `app/phone/sessions.py` (add `set_turn_evidence_path`, `set_summary`)
- Test: `tests/unit/test_phone_finalize.py`

**Interfaces:**
- Consumes: `PhoneSummaryProvider`, `CallSummary`, `CallSummaryContext`, `PhoneSummaryUnavailable` (Task 6); `send_telegram_message`, `render_call_notification`, `TelegramDeliveryError` (Task 7); `PhoneSummaryState` (Task 1); settings (Task 5).
- Produces:
  - `link_session_evidence(db: AsyncSession, session_id: UUID, evidence_dir: Path) -> int` — for each `<tid>.wav` under `<evidence_dir>/<session_id>/`, set `audio_evidence_path="<session_id>/<tid>.wav"` on the turn with `phonegate_transcript_id == tid`; returns the count linked.
  - `async build_summary_context(db: AsyncSession, session: CommunicationSession) -> CallSummaryContext`.
  - `async finalize_pending_calls() -> dict[str, int]` — the beat entry point; returns `{"picked", "done", "failed", "skipped"}`.
  - `SessionStore.set_summary(session, payload: dict[str, Any], state: PhoneSummaryState) -> None`, `SessionStore.set_turn_evidence_path(db, *, session_id, phonegate_transcript_id, path) -> None`.

- [ ] **Step 1: Write failing tests**

`tests/unit/test_phone_finalize.py` — build a closed auto-answered session with turns via the existing session/turn test helpers, put a WAV under `tmp_path/<sid>/<tid>.wav`, override settings (`phone_evidence_dir=tmp_path`, `phone_summary_llm_enabled`, batch etc.), and monkeypatch the provider:

```python
@pytest.mark.asyncio
async def test_finalize_links_evidence_even_when_llm_disabled(finalize_env):
    env = finalize_env(llm_enabled=False, with_clip_for_tid=7)
    result = await finalize_pending_calls()
    assert result["skipped"] == 1
    turn = await env.get_turn(phonegate_transcript_id=7)
    assert turn.audio_evidence_path == f"{env.session_id}/7.wav"
    assert (await env.get_session()).summary_state is PhoneSummaryState.SKIPPED


@pytest.mark.asyncio
async def test_finalize_writes_summary_and_bumps_needs_review(finalize_env, monkeypatch):
    env = finalize_env(llm_enabled=True)
    monkeypatch.setattr(
        "app.phone.summary.PhoneSummaryProvider.summarize",
        _fake_summarize(
            CallSummary(summary_text="итог", outcome_guess="interview_proposed", needs_review=True)
        ),
    )
    result = await finalize_pending_calls()
    assert result["done"] == 1
    s = await env.get_session()
    assert s.summary["summary_text"] == "итог"
    assert s.summary["hints"]["outcome_guess"] == "interview_proposed"
    assert s.needs_review is True
    assert s.summary_state is PhoneSummaryState.DONE
    assert s.summary["telegram"]["state"] == "disabled"


@pytest.mark.asyncio
async def test_finalize_retries_then_fails_after_max_attempts(finalize_env, monkeypatch):
    env = finalize_env(llm_enabled=True, max_attempts=2)
    monkeypatch.setattr(
        "app.phone.summary.PhoneSummaryProvider.summarize",
        _always_raise(PhoneSummaryUnavailable("http_503")),
    )
    await finalize_pending_calls()
    assert (await env.get_session()).summary_state is PhoneSummaryState.PENDING
    await finalize_pending_calls()
    s = await env.get_session()
    assert s.summary_state is PhoneSummaryState.FAILED
    assert s.summary["model_meta"]["attempts"] == 2


@pytest.mark.asyncio
async def test_finalize_skips_session_with_no_employer_turns(finalize_env):
    env = finalize_env(llm_enabled=True, employer_turns=0)
    await finalize_pending_calls()
    assert (await env.get_session()).summary_state is PhoneSummaryState.SKIPPED


@pytest.mark.asyncio
async def test_finalize_telegram_failure_keeps_done(finalize_env, monkeypatch):
    env = finalize_env(llm_enabled=True, telegram_enabled=True)
    monkeypatch.setattr(
        "app.phone.summary.PhoneSummaryProvider.summarize",
        _fake_summarize(CallSummary(summary_text="ок")),
    )
    monkeypatch.setattr(
        "app.phone.summary.send_telegram_message", _always_raise(TelegramDeliveryError("http_403"))
    )
    await finalize_pending_calls()
    s = await env.get_session()
    assert s.summary_state is PhoneSummaryState.DONE
    assert s.summary["telegram"]["state"] == "failed"
```

Provide the `finalize_env` fixture in this file (it builds the DB rows, writes the clip file, and applies a `get_settings` override via the same mechanism other phone unit tests use — check `tests/unit/test_phone_finalize.py` neighbours / `conftest.py`).

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_finalize.py -q`
Expected: FAIL — `ImportError: finalize_pending_calls`.

- [ ] **Step 3: `SessionStore` helpers**

`app/phone/sessions.py`:

```python
async def set_summary(
    self, call: CommunicationSession, payload: dict[str, Any], state: PhoneSummaryState
) -> None:
    call.summary = payload
    call.summary_state = state


async def set_turn_evidence_path(
    self, session: AsyncSession, *, session_id: UUID, phonegate_transcript_id: int, path: str
) -> None:
    turn = await session.scalar(
        select(CommunicationTurn).where(
            CommunicationTurn.session_id == session_id,
            CommunicationTurn.phonegate_transcript_id == phonegate_transcript_id,
        )
    )
    if turn is not None and turn.audio_evidence_path is None:
        turn.audio_evidence_path = path
```

- [ ] **Step 4: `link_session_evidence` in `evidence.py`**

```python
async def link_session_evidence(db: AsyncSession, session_id: UUID, evidence_dir: Path) -> int:
    session_dir = Path(evidence_dir) / str(session_id)
    if not session_dir.is_dir():
        return 0
    store = SessionStore()
    linked = 0
    for clip in sorted(session_dir.glob("*.wav")):
        try:
            transcript_id = int(clip.stem)
        except ValueError:
            continue
        await store.set_turn_evidence_path(
            db,
            session_id=session_id,
            phonegate_transcript_id=transcript_id,
            path=f"{session_id}/{clip.name}",
        )
        linked += 1
    return linked
```

(Import `AsyncSession`, `SessionStore`.)

- [ ] **Step 5: `build_summary_context` + `finalize_pending_calls` in `summary.py`**

```python
async def build_summary_context(
    db: AsyncSession, session: CommunicationSession
) -> CallSummaryContext:
    turns = list(
        (
            await db.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.session_id == session.id)
                .order_by(CommunicationTurn.seq)
            )
        ).all()
    )
    transcript = [
        (t.speaker.value, t.spoken_text if t.speaker is TurnSpeaker.ASSISTANT else t.text)
        for t in turns
        if t.speaker in (TurnSpeaker.ASSISTANT, TurnSpeaker.EMPLOYER)
    ]
    company = vacancy = None
    if session.canonical_job_id is not None:
        job = await db.get(CanonicalJob, session.canonical_job_id)
        if job is not None:
            company, vacancy = job.normalized_company, job.normalized_title
    profile = await db.get(UserProfile, session.profile_id)
    facts = dict(profile.confirmed_facts) if profile and profile.confirmed_facts else {}
    status = None
    if session.application_id is not None:
        app_row = await db.get(Application, session.application_id)
        status = app_row.status.value if app_row is not None else None
    return CallSummaryContext(
        transcript=transcript,
        company=company,
        vacancy=vacancy,
        application_status=status,
        confirmed_facts=facts,
    )


async def finalize_pending_calls() -> dict[str, int]:
    settings = get_settings()
    counters = {"picked": 0, "done": 0, "failed": 0, "skipped": 0}
    store = SessionStore()
    provider: PhoneSummaryProvider | None = None
    async with async_session_factory() as db:
        pending = list(
            (
                await db.scalars(
                    select(CommunicationSession)
                    .where(CommunicationSession.summary_state == PhoneSummaryState.PENDING)
                    .order_by(CommunicationSession.ended_at)
                    .limit(settings.phone_summary_batch)
                )
            ).all()
        )
        for session in pending:
            counters["picked"] += 1
            await link_session_evidence(db, session.id, settings.phone_evidence_dir)
            employer_turns = await db.scalar(
                select(func.count(CommunicationTurn.id)).where(
                    CommunicationTurn.session_id == session.id,
                    CommunicationTurn.speaker == TurnSpeaker.EMPLOYER,
                )
            )
            if not settings.phone_summary_llm_enabled or not employer_turns:
                session.summary_state = PhoneSummaryState.SKIPPED
                counters["skipped"] += 1
                await db.commit()
                continue
            if provider is None:
                api_key = settings.phone_summary_llm_api_key or settings.llmrouter_api_key
                provider = PhoneSummaryProvider(
                    base_url=settings.phone_summary_llm_base_url,
                    api_key=api_key.get_secret_value() if api_key else "",
                    model=settings.effective_summary_model,
                    prefer=settings.phone_summary_llm_prefer,
                    timeout_seconds=settings.phone_summary_llm_timeout_seconds,
                )
            attempts = int(session.summary.get("model_meta", {}).get("attempts", 0)) + 1
            try:
                result = await provider.summarize(await build_summary_context(db, session))
            except PhoneSummaryUnavailable as exc:
                session.summary = {
                    **session.summary,
                    "model_meta": {
                        **session.summary.get("model_meta", {}),
                        "attempts": attempts,
                        "last_error": str(exc),
                    },
                }
                if attempts >= settings.phone_summary_max_attempts:
                    session.summary_state = PhoneSummaryState.FAILED
                    counters["failed"] += 1
                await db.commit()
                continue
            payload = {
                "summary_text": result.summary_text,
                "hints": {
                    "mentioned_vacancy": result.mentioned_vacancy,
                    "proposed_datetime_text": result.proposed_datetime_text,
                    "proposed_address_text": result.proposed_address_text,
                    "contact_person_text": result.contact_person_text,
                    "outcome_guess": result.outcome_guess,
                },
                "model_meta": {
                    "provider": "llmrouter",
                    "model": settings.effective_summary_model,
                    "attempts": attempts,
                },
                "telegram": {"state": "pending"},
            }
            session.summary = payload
            if result.needs_review:
                session.needs_review = True
            session.summary_state = PhoneSummaryState.DONE
            counters["done"] += 1
            await _notify(session, result, settings)
            await db.commit()
    return counters


async def _notify(session: CommunicationSession, result: CallSummary, settings: Settings) -> None:
    if (
        not settings.telegram_enabled
        or settings.telegram_bot_token is None
        or not settings.telegram_chat_id
    ):
        session.summary = {**session.summary, "telegram": {"state": "disabled"}}
        return
    ctx = await _notify_context(session)  # small helper: company/vacancy for the message
    text = render_call_notification(
        result,
        company=ctx[0],
        vacancy=ctx[1],
        session_id=str(session.id),
        base_url=settings.public_base_url,
    )
    try:
        await send_telegram_message(
            token=settings.telegram_bot_token.get_secret_value(),
            chat_id=settings.telegram_chat_id,
            text=text,
        )
        session.summary = {
            **session.summary,
            "telegram": {"state": "sent", "sent_at": utcnow().isoformat()},
        }
    except TelegramDeliveryError as exc:
        logger.warning(
            "phone_telegram_delivery_failed", session_id=str(session.id), error=type(exc).__name__
        )
        session.summary = {**session.summary, "telegram": {"state": "failed", "error": str(exc)}}
```

Add the imports: `select`, `func` from sqlalchemy; `AsyncSession`; `async_session_factory` from `app.database`; `CommunicationSession`, `CommunicationTurn`, `CanonicalJob`, `UserProfile`, `Application` from `app.models.entities`; `TurnSpeaker`, `PhoneSummaryState` from `app.models.enums`; `SessionStore` from `app.phone.sessions`; `link_session_evidence` from `app.phone.evidence`; `send_telegram_message`, `render_call_notification`, `TelegramDeliveryError` from `app.phone.telegram`; `get_settings` from `app.settings`; `utcnow` from `app.database.base`; `Settings`. Verify `CanonicalJob` has `company_name` / `title` (adjust to the real column names).

- [ ] **Step 6: Run finalize tests — expect pass**

Run: `uv run pytest tests/unit/test_phone_finalize.py -q`
Expected: PASS.

- [ ] **Step 7: Lint + type + full phone suite**

Run: `uv run ruff check app tests && uv run mypy app && uv run pytest tests/unit -k phone -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/phone/summary.py app/phone/evidence.py app/phone/sessions.py tests/unit/test_phone_finalize.py
git commit -m "feat: finalize_pending_calls — link evidence, summarize, notify"
```

---

## Task 9: `prune_phone_evidence`

**Files:**
- Modify: `app/phone/evidence.py`
- Test: `tests/unit/test_phone_evidence.py`

**Interfaces:**
- Produces: `async prune_phone_evidence() -> dict[str, int]` returning `{"removed", "freed_bytes"}`; deletes `*.wav` older than `phone_evidence_retention_days` or beyond `phone_evidence_max_total_mb` (oldest-first), nulls the matching `communication_turns.audio_evidence_path`, removes empty `<session_id>/` dirs.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_prune_removes_by_age_and_nulls_pointer(prune_env):
    env = prune_env(retention_days=7)
    env.make_clip(session_dir="s1", tid=3, age_days=30)  # stale
    env.make_clip(session_dir="s1", tid=4, age_days=1)  # fresh
    await env.link(session_id="s1", tid=3)
    await env.link(session_id="s1", tid=4)
    result = await prune_phone_evidence()
    assert result["removed"] == 1
    assert not env.clip_exists("s1", 3) and env.clip_exists("s1", 4)
    assert (await env.turn("s1", 3)).audio_evidence_path is None
    assert (await env.turn("s1", 4)).audio_evidence_path is not None


@pytest.mark.asyncio
async def test_prune_enforces_total_size_cap_oldest_first(prune_env):
    env = prune_env(retention_days=365, max_total_mb=1)
    env.make_clip("s1", 1, age_days=3, size_bytes=700_000)
    env.make_clip("s1", 2, age_days=1, size_bytes=700_000)
    result = await prune_phone_evidence()
    assert result["removed"] == 1
    assert not env.clip_exists("s1", 1) and env.clip_exists("s1", 2)


@pytest.mark.asyncio
async def test_prune_noop_on_missing_root(prune_env):
    env = prune_env(dir_missing=True)
    assert await prune_phone_evidence() == {"removed": 0, "freed_bytes": 0}


@pytest.mark.asyncio
async def test_prune_tolerates_already_deleted_file(prune_env):
    env = prune_env(retention_days=7)
    env.make_clip("s1", 9, age_days=30)
    (env.root / "s1" / "9.wav").unlink()
    await prune_phone_evidence()  # must not raise
```

Provide `prune_env` in the file.

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_evidence.py -k prune -q`
Expected: FAIL — `ImportError: prune_phone_evidence`.

- [ ] **Step 3: Implement**

```python
async def prune_phone_evidence() -> dict[str, int]:
    settings = get_settings()
    root = Path(settings.phone_evidence_dir)
    if not root.is_dir():
        return {"removed": 0, "freed_bytes": 0}
    now = time.time()
    cutoff = now - settings.phone_evidence_retention_days * 86_400
    clips: list[tuple[Path, float, int]] = []
    for session_dir in root.iterdir():
        if not session_dir.is_dir():
            continue
        for clip in session_dir.glob("*.wav"):
            try:
                st = clip.stat()
            except OSError:
                continue
            clips.append((clip, st.st_mtime, st.st_size))
    to_remove = {c[0] for c in clips if c[1] < cutoff}
    survivors = sorted((c for c in clips if c[0] not in to_remove), key=lambda c: c[1])
    total = sum(c[2] for c in survivors)
    cap = settings.phone_evidence_max_total_mb * 1024 * 1024
    idx = 0
    while total > cap and idx < len(survivors):
        to_remove.add(survivors[idx][0])
        total -= survivors[idx][2]
        idx += 1
    freed = 0
    async with async_session_factory() as db:
        store = SessionStore()
        for path in to_remove:
            sid, tid = path.parent.name, path.stem
            try:
                await store.clear_turn_evidence_path(
                    db, session_id_text=sid, transcript_id_text=tid
                )
            except (ValueError, LookupError):
                pass
            try:
                freed += path.stat().st_size
            except OSError:
                pass
            path.unlink(missing_ok=True)
        await db.commit()
    for session_dir in list(root.iterdir()):
        if session_dir.is_dir() and not any(session_dir.iterdir()):
            session_dir.rmdir()
    return {"removed": len(to_remove), "freed_bytes": freed}
```

Add `SessionStore.clear_turn_evidence_path(db, *, session_id_text: str, transcript_id_text: str) -> None` in `sessions.py` (parse the UUID + int defensively; `UPDATE ... SET audio_evidence_path = NULL WHERE session_id = ? AND phonegate_transcript_id = ?`). Import `time`.

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/unit/test_phone_evidence.py -k prune -q`
Expected: PASS.

- [ ] **Step 5: Lint + type + commit**

```bash
uv run ruff check app tests && uv run mypy app && uv run pytest tests/unit/test_phone_evidence.py tests/unit/test_phone_sessions.py -q
git add app/phone/evidence.py app/phone/sessions.py tests/unit/test_phone_evidence.py
git commit -m "feat: prune_phone_evidence — age + total-size retention"
```

---

## Task 10: Celery wiring

**Files:**
- Modify: `app/scheduler/tasks.py`, `app/scheduler/celery_app.py`
- Modify: `docker-compose.yml`, `docker-compose.prod.yml`
- Test: `tests/unit/test_scheduler_tasks.py` (or the repo's scheduler test file)

**Interfaces:**
- Consumes: `finalize_pending_calls` (Task 8), `prune_phone_evidence` (Task 9).
- Produces: Celery tasks `job_agent.scheduler.finalize_pending_calls`, `job_agent.scheduler.prune_phone_evidence`; beat entries `finalize-pending-calls` (120 s) and `prune-phone-evidence` (`crontab(minute=40, hour=3)`), both on queue `phone`.

- [ ] **Step 1: Write failing task tests**

In the scheduler test file:

```python
def test_finalize_pending_calls_task_registered():
    assert "job_agent.scheduler.finalize_pending_calls" in celery_app.tasks


def test_prune_phone_evidence_task_registered():
    assert "job_agent.scheduler.prune_phone_evidence" in celery_app.tasks


def test_phone_beat_entries_present():
    bs = celery_app.conf.beat_schedule
    assert bs["finalize-pending-calls"]["options"]["queue"] == "phone"
    assert bs["prune-phone-evidence"]["options"]["queue"] == "phone"
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_scheduler_tasks.py -k "phone or finalize or prune" -q`
Expected: FAIL — `KeyError`.

- [ ] **Step 3: Add the task wrappers**

`app/scheduler/tasks.py`:

```python
@celery_app.task(name="job_agent.scheduler.finalize_pending_calls")
def finalize_pending_calls_task() -> dict[str, Any]:
    from app.phone.summary import finalize_pending_calls

    return _run_locked_periodic("phone-finalize", finalize_pending_calls(), ttl_seconds=600)


@celery_app.task(name="job_agent.scheduler.prune_phone_evidence")
def prune_phone_evidence_task() -> dict[str, Any]:
    from app.phone.evidence import prune_phone_evidence

    return _run_locked_periodic("phone-evidence-prune", prune_phone_evidence(), ttl_seconds=900)
```

- [ ] **Step 4: Beat + routes**

`app/scheduler/celery_app.py` — in `beat_schedule`:

```python
        "finalize-pending-calls": {
            "task": "job_agent.scheduler.finalize_pending_calls",
            "schedule": 120.0,
            "options": {"queue": "phone", "expires": 110},
        },
        "prune-phone-evidence": {
            "task": "job_agent.scheduler.prune_phone_evidence",
            "schedule": crontab(minute=40, hour=3),
            "options": {"queue": "phone"},
        },
```

In `task_routes`:

```python
        "job_agent.scheduler.finalize_pending_calls": {"queue": "phone"},
        "job_agent.scheduler.prune_phone_evidence": {"queue": "phone"},
```

- [ ] **Step 5: Compose worker queues**

In `docker-compose.yml` and `docker-compose.prod.yml`, find the `worker` service `command` (a `celery -A ... worker -Q ...` list) and append `,phone` to the `-Q` value. If the worker uses `-Q crawling,matching,applications,email,reports,maintenance`, it becomes `...,maintenance,phone`.

- [ ] **Step 6: Run task tests + compose config**

Run: `uv run pytest tests/unit/test_scheduler_tasks.py -k "phone or finalize or prune" -q && docker compose config --quiet`
Expected: PASS; compose config valid.

- [ ] **Step 7: Lint + type + commit**

```bash
uv run ruff check app && uv run mypy app
git add app/scheduler/tasks.py app/scheduler/celery_app.py docker-compose.yml docker-compose.prod.yml tests/unit/test_scheduler_tasks.py
git commit -m "feat: schedule finalize-pending-calls and prune-phone-evidence on the phone queue"
```

---

## Task 11: Refactor — extract `app/admin/phone_routes.py` (pure move)

**Files:**
- Create: `app/admin/phone_routes.py`
- Modify: `app/admin/routes.py`, `app/admin/__init__.py`
- Test: existing `tests/**` (must stay green — this task adds no behavior)

**Interfaces:**
- Produces: `app/admin/phone_routes.py` exports `router` (a `fastapi.APIRouter`) with the moved `POST /admin/phone/auto-answer/{action}` and `POST /admin/phone/call/{session_id}/{action}` handlers, and a module function `phone_health_context(session) -> dict[str, Any]` (the former `_phone_health`).
- `app/admin/routes.py` imports `phone_health_context` from `app.admin.phone_routes` and calls it where `_phone_health(...)` was called.

- [ ] **Step 1: Move `_phone_health`**

Cut `_phone_health` (`app/admin/routes.py:428`..end of function) into `app/admin/phone_routes.py` as `phone_health_context`. Keep imports it needs. In `routes.py`, `from app.admin.phone_routes import phone_health_context` and replace the call site (`phone_health = await _phone_health(session)` near line 1347) with `phone_health = await phone_health_context(session)`.

- [ ] **Step 2: Move the two POST handlers**

Cut `phone_auto_answer_toggle` (`routes.py:1661`) and `phone_call_action` (`routes.py:1687`) into `phone_routes.py`, attached to a local `router = APIRouter()`. They use `require_admin`, `require_csrf`, `_phone_redis`, `_audit_admin` — import those from `app.admin.routes` (or move the small shared helpers too if that creates a cycle; prefer importing from `routes`). Keep the route paths and redirect targets identical.

- [ ] **Step 3: Register the router**

`app/admin/__init__.py`:

```python
from app.admin.routes import router
from app.admin.phone_routes import router as phone_admin_router

router.include_router(phone_admin_router)

__all__ = ["router"]
```

(Or include it in `app/main.py` next to `admin_router` — match the repo's existing pattern; `router.include_router` on the admin router keeps one mount point.)

- [ ] **Step 4: Run the full admin + phone suites**

Run: `uv run pytest tests/unit -k "admin or phone" tests/integration -k "admin or interfaces" -q`
Expected: PASS — identical behavior, routes unchanged.

- [ ] **Step 5: Lint + type + commit**

```bash
uv run ruff check app tests && uv run mypy app
git add app/admin/phone_routes.py app/admin/routes.py app/admin/__init__.py
git commit -m "refactor: extract phone admin handlers into app/admin/phone_routes.py"
```

---

## Task 12: `Звонки` section — nav, page shell, Live + История tabs, badge

**Files:**
- Modify: `app/admin/phone_routes.py` (add `build_calls_context`)
- Modify: `app/admin/routes.py` (`_VIEW_TITLES`, `elif view == "calls"`, `counts["phone_review"]`)
- Modify: `app/admin/templates/dashboard.html`
- Create: `app/admin/templates/calls.html`, `_calls_live.html`, `_calls_history.html`
- Test: `tests/unit/test_phone_admin_calls.py`

**Interfaces:**
- Consumes: `phone_health_context` (Task 11), `_pagination` (`routes.py:415`), `mask_phone`, `PhoneSummaryState`.
- Produces: `async build_calls_context(session, *, tab: str, page: int, filter_: str, query: str) -> dict[str, Any]` returning `{"tab", "calls_health", "active_call", "call_rows", "pagination", "filter", "query", "detail"}`. `counts["phone_review"] = <count of channel=call sessions with needs_review or summary_state='failed'>`.

- [ ] **Step 1: Write failing context tests**

`tests/unit/test_phone_admin_calls.py`:

```python
@pytest.mark.asyncio
async def test_calls_context_history_lists_sessions_newest_first(admin_client, seeded_calls):
    ctx = await build_calls_context(seeded_calls.db, tab="history", page=1, filter_="all", query="")
    assert [r["id"] for r in ctx["call_rows"]] == seeded_calls.newest_first_ids


@pytest.mark.asyncio
async def test_calls_context_filter_needs_review(admin_client, seeded_calls):
    ctx = await build_calls_context(
        seeded_calls.db, tab="history", page=1, filter_="needs_review", query=""
    )
    assert all(r["needs_review"] for r in ctx["call_rows"])


@pytest.mark.asyncio
async def test_calls_context_search_by_company(admin_client, seeded_calls):
    ctx = await build_calls_context(
        seeded_calls.db, tab="history", page=1, filter_="all", query="Example"
    )
    assert ctx["call_rows"] and all("Example" in (r["company"] or "") for r in ctx["call_rows"])


@pytest.mark.asyncio
async def test_calls_view_renders(admin_client):
    resp = await admin_client.get("/?view=calls&tab=history")
    assert resp.status_code == 200
    assert "Звонки" in resp.text


def test_calls_in_view_titles():
    from app.admin.routes import _VIEW_TITLES

    assert _VIEW_TITLES["calls"] == "Звонки"
```

(`admin_client` / an authenticated test client + `seeded_calls` fixture — mirror `tests/integration/test_gmail_oauth_routes.py` / the admin dashboard tests for the auth setup.)

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_admin_calls.py -q`
Expected: FAIL — `_VIEW_TITLES` has no `calls`, `build_calls_context` missing.

- [ ] **Step 3: `_VIEW_TITLES` + view branch + badge**

`app/admin/routes.py`:
- `_VIEW_TITLES`: add `"calls": "Звонки"`.
- In `dashboard()` where `phone_health` is built for `diagnostics`, add a branch:

```python
    elif view == "calls":
        from app.admin.phone_routes import build_calls_context

        calls_ctx = await build_calls_context(
            session,
            tab=(request.query_params.get("tab") or "live"),
            page=int(request.query_params.get("page", "1") or "1"),
            filter_=(request.query_params.get("filter") or "all"),
            query=(request.query_params.get("q") or "").strip(),
        )
```

  and merge `calls_ctx` into the template context dict at `routes.py:~1442`.
- Add `counts["phone_review"]`:

```python
counts["phone_review"] = int(
    await session.scalar(
        select(func.count(CommunicationSession.id)).where(
            CommunicationSession.channel == CommunicationChannel.CALL,
            or_(
                CommunicationSession.needs_review.is_(True),
                CommunicationSession.summary_state == PhoneSummaryState.FAILED,
            ),
        )
    )
    or 0
)
```

- [ ] **Step 4: `build_calls_context` in `phone_routes.py`**

Implement per the Interfaces block. History query:

```python
async def build_calls_context(session, *, tab, page, filter_, query):
    ctx: dict[str, Any] = {
        "tab": tab if tab in {"live", "history", "evidence"} else "live",
        "filter": filter_,
        "query": query,
    }
    ctx["calls_health"] = await phone_health_context(session)
    if ctx["tab"] == "history":
        stmt = (
            select(CommunicationSession)
            .where(CommunicationSession.channel == CommunicationChannel.CALL)
            .order_by(CommunicationSession.started_at.desc())
        )
        stmt = _apply_call_filter(stmt, filter_)
        if query:
            like = f"%{query}%"
            stmt = stmt.outerjoin(
                CanonicalJob, CommunicationSession.canonical_job_id == CanonicalJob.id
            ).where(
                or_(
                    CanonicalJob.normalized_company.ilike(like),
                    CanonicalJob.normalized_title.ilike(like),
                    CommunicationSession.remote_address.ilike(like),
                )
            )
        per_page = 25
        total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = list(
            (await session.scalars(stmt.limit(per_page).offset((page - 1) * per_page))).all()
        )
        ctx["call_rows"] = [await _call_row(session, r) for r in rows]
        ctx["pagination"] = _pagination(total, page, per_page)
    # live tab uses ctx["calls_health"]["auto_answer"] / ["active_call"] already built
    return ctx
```

`_call_row(session, r)` returns `{id, started_at, company, vacancy, direction, duration_s, outcome, auto_answered, needs_review, summary_state, telegram_state}` (resolve company/vacancy via `canonical_job_id` → `CanonicalJob.normalized_company` / `.normalized_title`; `telegram_state = r.summary.get("telegram", {}).get("state")`). `_apply_call_filter` maps `needs_review` → `needs_review.is_(True)`, `interview_proposed` → `summary["hints"]["outcome_guess"] == "interview_proposed"` (use `CommunicationSession.summary["hints"]["outcome_guess"].as_string()` for Postgres JSON; for SQLite test DB use `func.json_extract`), `missed_dropped` → `outcome.in_([MISSED, ABANDONED])`, `unknown_caller` → `application_id.is_(None)`, `all` → no-op.

> JSON-path filtering differs between SQLite (tests) and Postgres (prod). Keep `interview_proposed` filtering in Python on the already-fetched rows if a portable SQL expression is awkward — fetch, then filter the list, then paginate the filtered list. Document whichever you pick in a code comment.

- [ ] **Step 5: Templates**

`calls.html` (extends `base.html`, `{% block body %}` mirrors `dashboard.html`'s shell — reuse its sidebar include by refactoring the sidebar into a `_sidebar.html` partial that both `dashboard.html` and `calls.html` include, OR keep `calls.html` rendered *inside* `dashboard.html`'s existing `{% elif view == 'calls' %}{% include "calls.html" %}`). Prefer the latter — one line in `dashboard.html`:

```jinja
{% elif view == 'calls' %}{% include "calls.html" %}
```

`dashboard.html` nav — add after the diagnostics link (line ~13):

```jinja
      <a class="nav-link {% if view == 'calls' %}is-active{% endif %}" href="/?view=calls&profile_id={{ selected_profile_id }}"><span class="nav-icon">☎</span><span>Звонки</span>{% if counts.phone_review %}<span class="nav-count">{{ counts.phone_review }}</span>{% endif %}</a>
```

`calls.html`:

```jinja
<div class="admin-section">
  <div class="section-heading"><h1>Звонки</h1></div>
  {% include "_phone_health.html" %}
  <div class="filter-panel"><div class="tabs">
    <a class="tab {% if tab == 'live' %}is-active{% endif %}" href="/?view=calls&tab=live">Live</a>
    <a class="tab {% if tab == 'history' %}is-active{% endif %}" href="/?view=calls&tab=history">История</a>
    <a class="tab {% if tab == 'evidence' %}is-active{% endif %}" href="/?view=calls&tab=evidence">Evidence</a>
    <span class="tab" style="opacity:.4;pointer-events:none">Собеседования · после Phase 4</span>
  </div></div>
  {% if tab == 'live' %}{% include "_calls_live.html" %}
  {% elif tab == 'history' %}{% include "_calls_history.html" %}
  {% elif tab == 'evidence' %}{% include "_calls_evidence.html" %}{% endif %}
</div>
```

`_calls_live.html` — render `calls_health.auto_answer` badges + stop/resume form (copy from `_phone_health.html`) and, when `calls_health.active_call`, the masked caller / company / `script_stage` / current-session transcript (pass the current session's turns in the context) + the existing hangup/mute forms. Add `<meta http-equiv="refresh" content="5">` guarded by `{% if calls_health.active_call %}` via a `{% block head %}` — since `calls.html` is included inside `dashboard.html`, instead put `<div hx-noop>` — actually add the refresh `<meta>` into `dashboard.html`'s `{% block head %}` guarded by `view == 'calls' and calls_health and calls_health.active_call`.

`_calls_history.html` — the search form (`<form method="get">` with hidden `view=calls`, `tab=history`, a `<select name="filter">`, `<input type="search" name="q">`), a `.queue-list` of `call_rows` (each a `.queue-card` linking to `/?view=calls&tab=history&session={{ row.id }}`), and the `.pagination` block (copy the markup already used in `dashboard_history.html`).

- [ ] **Step 6: Run admin calls tests**

Run: `uv run pytest tests/unit/test_phone_admin_calls.py -q`
Expected: PASS.

- [ ] **Step 7: Lint + type + full admin suite**

Run: `uv run ruff check app tests && uv run mypy app && uv run pytest tests/unit -k "admin or phone" -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/admin/ tests/unit/test_phone_admin_calls.py
git commit -m "feat: Звонки admin section — nav, Live and История tabs"
```

---

## Task 13: Session detail view (summary block, transcript, evidence player, audit)

**Files:**
- Modify: `app/admin/phone_routes.py` (`build_calls_context` detail branch)
- Create: `app/admin/templates/_calls_history_detail.html`
- Modify: `app/admin/templates/_calls_history.html` (render detail when `detail` present)
- Test: `tests/unit/test_phone_admin_calls.py`

**Interfaces:**
- Consumes: `build_calls_context` (Task 12).
- Produces: when `build_calls_context` receives a `session=<uuid>` param, `ctx["detail"]` = `{session, turns: [...], summary, audit_events: [...]}` where each turn dict includes `audio_evidence_url` (`/admin/phone/evidence/<sid>/<tid>.wav` when `audio_evidence_path` set, else `None`).

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_calls_detail_exposes_summary_and_evidence_url(admin_client, seeded_calls):
    ctx = await build_calls_context(
        seeded_calls.db,
        tab="history",
        page=1,
        filter_="all",
        query="",
        session_id=seeded_calls.summarized_id,
    )
    d = ctx["detail"]
    assert d["summary"]["summary_text"]
    assert any(t["audio_evidence_url"] for t in d["turns"])


@pytest.mark.asyncio
async def test_calls_detail_page_renders(admin_client, seeded_calls):
    resp = await admin_client.get(f"/?view=calls&tab=history&session={seeded_calls.summarized_id}")
    assert resp.status_code == 200 and "Итог звонка" in resp.text
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_admin_calls.py -k detail -q`
Expected: FAIL — `build_calls_context` has no `session_id` param.

- [ ] **Step 3: Add the `session_id` param + detail branch**

Extend `build_calls_context(..., session_id: str | None = None)`. In `routes.py`'s `elif view == "calls"` pass `session_id=request.query_params.get("session")`. When `session_id` is a valid UUID and the session exists:

```python
call = await session.get(CommunicationSession, uuid.UUID(session_id))
if call is not None:
    turns = list(
        (
            await session.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.session_id == call.id)
                .order_by(CommunicationTurn.seq)
            )
        ).all()
    )
    audits = list(
        (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.entity_id == str(call.id))
                .order_by(AuditEvent.timestamp)
            )
        ).all()
    )
    ctx["detail"] = {
        "session": _call_row(session, call),  # plus script_stage, diagnostics, rx_frame_stats
        "summary": call.summary,
        "summary_state": call.summary_state.value,
        "turns": [
            {
                "seq": t.seq,
                "speaker": t.speaker.value,
                "text": t.spoken_text if t.speaker is TurnSpeaker.ASSISTANT else t.text,
                "delivery_status": t.delivery_status.value,
                "asr_confidence": t.asr_confidence,
                "audio_evidence_url": (
                    f"/admin/phone/evidence/{call.id}/{t.phonegate_transcript_id}.wav"
                    if t.audio_evidence_path
                    else None
                ),
            }
            for t in turns
        ],
        "audit_events": [{"action": a.action, "at": a.timestamp.isoformat()} for a in audits],
    }
```

- [ ] **Step 4: `_calls_history_detail.html`**

Renders: header (masked caller, company/vacancy, times, outcome, `script_stage`, `auto_answered`, `needs_review`); a "Итог звонка" panel (`detail.summary.summary_text` + a `<table>` of `detail.summary.hints` + muted `detail.summary.model_meta` + a Telegram-state badge); the transcript list (employer neutral, assistant with a `delivery_status` badge; when `turn.audio_evidence_url` render `<audio controls preload="none" src="{{ turn.audio_evidence_url }}"></audio>`); a `<details>` block with `diagnostics` + `rx_frame_stats`; a muted `audit_events` list. `_calls_history.html` renders `{% if detail %}{% include "_calls_history_detail.html" %}{% else %}<the list>{% endif %}`.

- [ ] **Step 5: Run detail tests**

Run: `uv run pytest tests/unit/test_phone_admin_calls.py -k detail -q`
Expected: PASS.

- [ ] **Step 6: Lint + type + commit**

```bash
uv run ruff check app tests && uv run mypy app && uv run pytest tests/unit -k phone -q
git add app/admin/ tests/unit/test_phone_admin_calls.py
git commit -m "feat: Звонки session detail — summary, transcript, evidence player, audit"
```

---

## Task 14: Evidence WAV stream endpoint + Evidence tab

**Files:**
- Modify: `app/admin/phone_routes.py`
- Create: `app/admin/templates/_calls_evidence.html`
- Test: `tests/unit/test_phone_admin_calls.py`

**Interfaces:**
- Produces: `GET /admin/phone/evidence/{session_id}/{transcript_id}.wav` (admin-auth) → `FileResponse`/`Response` `audio/wav`; `404` when the file is missing or the path escapes `phone_evidence_dir`. `build_calls_context` `evidence` tab branch → `ctx["evidence_rows"] = [{session_id, company, started_at, seq, text, asr_confidence, created_at, expires_at, url}]`.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_evidence_stream_returns_wav(admin_client, seeded_calls, tmp_evidence):
    tmp_evidence.write(seeded_calls.summarized_id, tid=7, data=b"RIFFxx")
    resp = await admin_client.get(f"/admin/phone/evidence/{seeded_calls.summarized_id}/7.wav")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("audio/wav")
    assert resp.content == b"RIFFxx"


@pytest.mark.asyncio
async def test_evidence_stream_404_when_missing(admin_client, seeded_calls):
    resp = await admin_client.get(f"/admin/phone/evidence/{seeded_calls.summarized_id}/999.wav")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_evidence_stream_rejects_path_traversal(admin_client):
    resp = await admin_client.get("/admin/phone/evidence/..%2f..%2fetc/0.wav")
    assert resp.status_code in (404, 422)


@pytest.mark.asyncio
async def test_evidence_stream_requires_auth(unauth_client, seeded_calls):
    resp = await unauth_client.get(f"/admin/phone/evidence/{seeded_calls.summarized_id}/7.wav")
    assert resp.status_code in (302, 401, 403)


@pytest.mark.asyncio
async def test_evidence_tab_lists_clips(admin_client, seeded_calls, tmp_evidence):
    tmp_evidence.write(seeded_calls.summarized_id, tid=7, data=b"RIFFxx")
    ctx = await build_calls_context(
        seeded_calls.db, tab="evidence", page=1, filter_="all", query=""
    )
    assert ctx["evidence_rows"] and ctx["evidence_rows"][0]["url"].endswith("/7.wav")
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_admin_calls.py -k evidence -q`
Expected: FAIL — route 404 (not registered).

- [ ] **Step 3: Implement the endpoint**

`app/admin/phone_routes.py`, on the module `router`:

```python
@router.get("/admin/phone/evidence/{session_id}/{transcript_id}.wav")
async def stream_evidence_clip(
    session_id: str,
    transcript_id: str,
    request: Request,
) -> Response:
    require_admin(request)
    try:
        sid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    if not transcript_id.isdigit():
        raise HTTPException(status_code=404)
    root = Path(get_settings().phone_evidence_dir).resolve()
    target = (root / str(sid) / f"{transcript_id}.wav").resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404)
    return Response(
        target.read_bytes(),
        media_type="audio/wav",
        headers={"Cache-Control": "private, max-age=60"},
    )
```

- [ ] **Step 4: Evidence tab context + template**

`build_calls_context` `evidence` branch: `select(CommunicationTurn).where(audio_evidence_path.is_not(None)).order_by(CommunicationTurn.session_id, seq)`, join the session for company/started_at; per row compute `created_at` and `expires_at` from the file mtime + `phone_evidence_retention_days` (skip rows whose file no longer exists). `_calls_evidence.html`: group by session, each clip an `<audio controls preload="none">` + text + `asr_confidence` + `создан` / `истекает` + link to the session detail.

- [ ] **Step 5: Run — expect pass**

Run: `uv run pytest tests/unit/test_phone_admin_calls.py -k evidence -q`
Expected: PASS.

- [ ] **Step 6: Lint + type + commit**

```bash
uv run ruff check app tests && uv run mypy app && uv run pytest tests/unit -k phone -q
git add app/admin/ tests/unit/test_phone_admin_calls.py
git commit -m "feat: evidence WAV stream endpoint + Evidence tab"
```

---

## Task 15: Extend `GET /api/v1/phone/sessions[/{id}]` with 2b fields + filters

**Files:**
- Modify: `app/api/phone_routes.py`
- Test: `tests/unit/test_phone_api.py`

**Interfaces:**
- Consumes: `PhoneSummaryState`.
- Produces: `_session_row` gains `auto_answered`, `script_stage`, `summary_state`. `list_sessions` gains `filter: str = "all"` and `q: str = ""` query params (same semantics as `build_calls_context`). `session_detail` response gains `summary`, `summary_state`, and per-turn `audio_evidence_path`.

- [ ] **Step 1: Write failing tests**

`tests/unit/test_phone_api.py`:

```python
@pytest.mark.asyncio
async def test_sessions_list_includes_2b_fields(api_client, seeded_calls):
    body = (await api_client.get("/api/v1/phone/sessions")).json()
    row = next(r for r in body["sessions"] if r["id"] == seeded_calls.summarized_id)
    assert row["auto_answered"] is True and row["summary_state"] == "done"


@pytest.mark.asyncio
async def test_sessions_list_filter_needs_review(api_client, seeded_calls):
    body = (await api_client.get("/api/v1/phone/sessions?filter=needs_review")).json()
    assert all(r["needs_review"] for r in body["sessions"])


@pytest.mark.asyncio
async def test_session_detail_includes_summary_and_evidence(api_client, seeded_calls):
    body = (await api_client.get(f"/api/v1/phone/sessions/{seeded_calls.summarized_id}")).json()
    assert body["summary"]["summary_text"]
    assert body["summary_state"] == "done"
    assert any(t.get("audio_evidence_path") for t in body["turns"])
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_phone_api.py -k "2b or filter or evidence" -q`
Expected: FAIL — missing keys.

- [ ] **Step 3: Implement**

- `_session_row`: add `"auto_answered": call.auto_answered`, `"script_stage": call.script_stage`, `"summary_state": call.summary_state.value`.
- `list_sessions(...)`: add `filter: str = "all"`, `q: str = ""` params; apply the same filter/search as `build_calls_context` (extract a shared `_filtered_call_query(stmt, filter_, q)` helper into `app/phone/sessions.py` or a new `app/phone/queries.py` and use it from both `phone_routes.py` files — DRY).
- `session_detail(...)`: add `"summary": call.summary`, `"summary_state": call.summary_state.value`; in the turn dict add `"audio_evidence_path": t.audio_evidence_path`.

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/unit/test_phone_api.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + type + commit**

```bash
uv run ruff check app tests && uv run mypy app && uv run pytest tests/unit -k phone -q
git add app/api/phone_routes.py app/phone/ tests/unit/test_phone_api.py
git commit -m "feat: expose 2b fields and filters on /api/v1/phone/sessions"
```

---

## Task 16: Integration E2E + final sweep

**Files:**
- Create: `tests/integration/test_phone_evidence_e2e.py`
- Test: full suite

**Interfaces:**
- Consumes: everything.

- [ ] **Step 1: Write the E2E test**

`tests/integration/test_phone_evidence_e2e.py` — drive the orchestrator against `FakePhoneGate` end to end (model it on `tests/unit/test_phone_orchestrator_loop.py` and any existing `tests/integration` phone test):

```python
@pytest.mark.asyncio
async def test_evidence_capture_then_finalize_links_and_summarizes(phone_e2e_env, monkeypatch):
    env = phone_e2e_env(auto_answer=True, summary_enabled=True)
    env.fake.set_call_audio(b"RIFF....WAVEdata")
    async with env.run_agent():
        env.fake.ring("+37360000000")
        await env.wait_for_state("IN_CALL")
        await env.wait_for_script_stage("listening")
        env.fake.transcript("rx", "в четверг в 14:00 на Индустриальной 12")
        await env.wait_for_evidence_clip()  # file appears under storage/phone_evidence/<sid>/
        env.fake.hangup()
        await env.wait_for_session_closed()
    # session is now summary_state='pending'
    monkeypatch.setattr(
        "app.phone.summary.PhoneSummaryProvider.summarize",
        _fake_summarize(
            CallSummary(
                summary_text="Собеседование в четверг.",
                outcome_guess="interview_proposed",
                proposed_datetime_text="в четверг в 14:00",
            )
        ),
    )
    result = await finalize_pending_calls()
    assert result["done"] == 1
    session = await env.get_session()
    assert session.summary_state is PhoneSummaryState.DONE
    turn = await env.get_turn_with_evidence()
    assert turn.audio_evidence_path is not None
    assert (Path(env.settings.phone_evidence_dir) / turn.audio_evidence_path).is_file()
```

- [ ] **Step 2: Run the E2E test**

Run: `uv run pytest tests/integration/test_phone_evidence_e2e.py -q`
Expected: PASS.

- [ ] **Step 3: Full sweep**

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app fixture_site
uv run pytest
docker compose config --quiet
```

Expected: all PASS. Fix any fallout (most likely: `test_phone_api.py` / admin dashboard snapshot-style assertions that enumerate keys, and `ruff format` on new files — run `uv run ruff format .` and re-commit).

- [ ] **Step 4: DEV Postgres migration check**

Run:

```bash
RUN_SERVICE_INTEGRATION_TESTS=1 \
DATABASE_URL=postgresql+asyncpg://job_agent:<dev-pw>@127.0.0.1:55432/job_agent_test \
uv run alembic upgrade head && uv run alembic check
```

Expected: upgrades cleanly; `alembic check` → "No new upgrade operations detected."

- [ ] **Step 5: Commit + wrap-up**

```bash
git add tests/integration/test_phone_evidence_e2e.py
git commit -m "test: phone 2b end-to-end — capture, close, finalize, link, summarize"
```

Then update `docs/superpowers/specs/2026-09-05-phone-call-agent-phase-2b-design.md` §9.4 checklist marks and note in the PR description: real-call assertion (§9.3) and the manual acceptance run are recommended before flipping `phone_summary_llm_enabled` / `telegram_enabled` in production.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §3.1–§3.5 schema, migration, head tests | 1 |
| §4.5 `recent_call_audio` + FakePhoneGate | 2 |
| §4.1–§4.4 `EvidenceCapturer` + heuristic + cap | 3 |
| §4.2 orchestrator hook; §3.4 `close`→pending | 4 |
| §5.1 settings; §6.1 Telegram settings; §7.3 evidence settings; production validation; `.env.example` | 3 (partial), 5 |
| §5.1–§5.2 `PhoneSummaryProvider` + `CallSummary` | 6 |
| §6.2–§6.3 Telegram sender + templates; security/threat-model notes | 7 |
| §4.6 evidence linking; §5.3–§5.4 context + `finalize_pending_calls`; §6.4 `_notify` | 8 |
| §7.1 `prune_phone_evidence` | 9 |
| §5.5, §7.2 Celery wiring + `phone` queue + compose | 10 |
| §8.1 `app/admin/phone_routes.py` refactor | 11 |
| §8.2 nav+badge; §8.3 Live + История tabs | 12 |
| §8.3 session detail | 13 |
| §8.3 Evidence tab; §8.4 WAV stream endpoint | 14 |
| §8.5 API extension | 15 |
| §9.2 integration E2E; §9.4 final sweep | 16 |
| §9.3 real-call assertion | Noted in Task 16 Step 5 as recommended-not-gating (spec §9.3) |
| §10 invariants | Global Constraints + enforced per task |

No spec section is left without a task.

**Placeholder scan:** the plan defers a few fixture bodies (`finalize_env`, `prune_env`, `seeded_calls`, `phone_e2e_env`, `_fake_summarize`, `_always_raise`) to "provide it in the file, mirroring neighbouring tests" rather than writing every line — acceptable because the repo's phone test suite already has close analogues (`test_phone_orchestrator_loop.py`, `test_phone_finalize.py` neighbours) and the interface each fixture must satisfy is stated. The `interview_proposed` JSON-path filter has an explicit "pick one, comment it" instruction with a portable fallback (filter in Python). No `TODO` / "add error handling" / "similar to Task N" placeholders remain.

**Type consistency:** `summary_state` is `PhoneSummaryState` everywhere; `CallSummary` field names are identical across Tasks 6/7/8; `finalize_pending_calls() -> dict[str, int]` with keys `picked/done/failed/skipped` consistent between Task 8 impl and Task 8/16 tests; `audio_evidence_path` value format `"<session_id>/<transcript_id>.wav"` consistent across Tasks 4/8/9/13/14; the evidence URL `/admin/phone/evidence/<sid>/<tid>.wav` consistent across Tasks 13/14; `recent_call_audio(seconds: int) -> bytes` consistent across Tasks 2/3.

**Known follow-ups the executor must resolve against live code (flagged in-task):** the worker `-Q` list value in the compose files (Task 10 Step 5); the admin authenticated-test-client / auth fixture name, modelled on `tests/integration/test_gmail_oauth_routes.py` (Tasks 12–14); the exact scheduler test file name under `tests/unit/` (Task 10); whether the new admin router is best included on the admin `router` or in `app/main.py` (Task 11 Step 3).
