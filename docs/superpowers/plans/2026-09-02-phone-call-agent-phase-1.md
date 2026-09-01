# Phone Call Agent — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give JobHunter a read-only integration layer that observes real
PhoneGate call activity and persists it as correlated call sessions and
transcripts, so later phases build dialogue and scheduling on stable tables.

**Architecture:** A dedicated long-lived asyncio process (`job-agent
phone-agent`, Compose service `call-agent`) holds one persistent HTTP client to
PhoneGate's REST API, polls `/api/events` + `/api/device/status`, correlates each
inbound call to a JobHunter `Application` / profile, and writes
`CommunicationSession` + `CommunicationTurn` rows. It issues only `GET` requests
— it never answers, speaks, or dials. A separate `phone_channel_health` table,
three read-only `/api/v1/phone/*` endpoints, and a section in the admin
Диагностика view surface the channel state. The process shares the `app`
package, `Settings`, and DB session factory with the API and Celery worker and
adds no Celery dependency.

**Tech Stack:** Python 3.12, asyncio, httpx (async, `ASGITransport` for tests),
SQLAlchemy 2 async + Alembic, pydantic v2 / pydantic-settings, FastAPI, Redis
(async client for the cursor, sync `leased_redis_lock` for the singleton),
structlog, `phonenumbers`, pytest + pytest-asyncio, ruff, mypy (strict).

**Spec:** `docs/superpowers/specs/2026-09-02-phone-call-agent-phase-1-design.md`
— read it alongside this plan. The plan argues from the spec; section references
like "(spec §7.2)" point back to it.

## Global Constraints

- Python 3.12 semantics. `from __future__ import annotations` in every new module.
- Before handoff: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy app fixture_site`, `uv run pytest`. All must pass.
- All new code fully typed; `app/phone/` passes `mypy` strict with no `ignore`.
- The call agent is a **pure observer of PhoneGate**: only `GET` requests. No
  `answer` / `speak` / `dial` / `send_sms` client methods exist in Phase 1.
- Never enable real email delivery or live crawling in tests. No test makes a
  real network call to PhoneGate; the opt-in live smoke is off in CI.
- `app/observability/health.py::readiness_status()` is **not** modified. Phone
  degradation must never change `/ready`.
- Caller numbers are masked in logs and `AuditEvent` details as `+373••••NNN`
  (keep country code + last 3 digits).
- `uv run alembic upgrade head && uv run alembic check` must report
  "No new upgrade operations detected." after the migration task.
- Commits use conventional prefixes (`feat:`, `fix:`, `test:`, `docs:`),
  English, present tense. Repo docs are Russian; commit messages are English.
- Work on branch `feature/phone-call-agent` (already checked out).
- End every commit message body with:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01KZa4B2AZAPZsKTVsQbnLSR
  ```

---

## Task 1: New enums

**Files:**
- Modify: `app/models/enums.py`
- Test: `tests/unit/test_phone_enums.py` (create)

**Interfaces:**
- Produces: `ContactType.PHONE`; enums `CommunicationChannel`,
  `CommunicationDirection`, `CommunicationOutcome`, `TurnSpeaker`,
  `CallFactState`, `InterviewFormat`, `InterviewStatus`, `PhoneComponentStatus`
  — all `StrEnum`, values are the lowercase member names except where noted.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_enums.py`:

```python
from __future__ import annotations

from app.models.enums import (
    CallFactState,
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    ContactType,
    InterviewFormat,
    InterviewStatus,
    PhoneComponentStatus,
    TurnSpeaker,
)


def test_contact_type_has_phone() -> None:
    assert ContactType.PHONE == "phone"


def test_communication_enums_values() -> None:
    assert CommunicationChannel.CALL == "call"
    assert CommunicationDirection.INBOUND == "inbound"
    assert set(CommunicationOutcome) == {"missed", "completed", "abandoned", "unknown"}
    assert set(TurnSpeaker) == {"employer", "assistant", "operator", "system"}


def test_fact_and_interview_enums_values() -> None:
    assert set(CallFactState) == {"candidate", "confirmed", "conflict", "unknown"}
    assert set(InterviewFormat) == {"onsite", "remote", "phone", "unknown"}
    assert set(InterviewStatus) == {"proposed", "confirmed", "needs_review", "cancelled"}
    assert set(PhoneComponentStatus) == {"healthy", "degraded", "unavailable", "unknown"}
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_phone_enums.py -q`
Expected: FAIL — `ImportError` / `AttributeError: PHONE`.

- [ ] **Step 3: Implement**

In `app/models/enums.py`, add `PHONE = "phone"` as the last member of
`ContactType`, then append these classes at the end of the file:

```python
class CommunicationChannel(StrEnum):
    CALL = "call"
    SMS = "sms"


class CommunicationDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CommunicationOutcome(StrEnum):
    MISSED = "missed"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


class TurnSpeaker(StrEnum):
    EMPLOYER = "employer"
    ASSISTANT = "assistant"
    OPERATOR = "operator"
    SYSTEM = "system"


class CallFactState(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class InterviewFormat(StrEnum):
    ONSITE = "onsite"
    REMOTE = "remote"
    PHONE = "phone"
    UNKNOWN = "unknown"


class InterviewStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"


class PhoneComponentStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `uv run pytest tests/unit/test_phone_enums.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models/enums.py tests/unit/test_phone_enums.py
git commit -m "feat: add phone/communication enums and ContactType.PHONE"
```

---

## Task 2: ORM entities

**Files:**
- Modify: `app/models/entities.py`
- Test: `tests/unit/test_phone_entities.py` (create)

**Interfaces:**
- Consumes: enums from Task 1; `Base`, `UUIDPrimaryKeyMixin`, `TimestampMixin`,
  `utcnow` from `app.database.base`; `enum_column` helper already in
  `entities.py`.
- Produces: `CommunicationSession`, `CommunicationTurn`, `CallFact`,
  `InterviewAppointment`, `PhoneChannelHealth` mapped classes with the tables and
  columns from spec §6.2–§6.6.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_entities.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, AsyncSession

from app.models.entities import (
    CommunicationSession,
    CommunicationTurn,
    PhoneChannelHealth,
    UserProfile,
)
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    PhoneComponentStatus,
    TurnSpeaker,
)


@pytest_asyncio.fixture
async def db(sqlite_session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    async with sqlite_session_factory() as session:
        yield session


async def test_session_and_turn_roundtrip(db: AsyncSession) -> None:
    profile = UserProfile(name="Основной", is_default=True)
    db.add(profile)
    await db.flush()

    call = CommunicationSession(
        profile_id=profile.id,
        channel=CommunicationChannel.CALL,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        remote_address="+37360111222",
        remote_raw="+37360111222",
        phonegate_event_id_start=5,
        started_at=datetime.now(UTC),
    )
    db.add(call)
    await db.flush()

    db.add(
        CommunicationTurn(
            session_id=call.id,
            phonegate_transcript_id=1,
            seq=1,
            speaker=TurnSpeaker.EMPLOYER,
            text="Здравствуйте",
            occurred_at=datetime.now(UTC),
        )
    )
    await db.flush()

    health = PhoneChannelHealth(
        component="phonegate_transport",
        status=PhoneComponentStatus.HEALTHY,
        updated_at=datetime.now(UTC),
    )
    db.add(health)
    await db.commit()

    loaded = await db.get(CommunicationSession, call.id)
    assert loaded is not None
    assert loaded.direction == CommunicationDirection.INBOUND
    assert loaded.ended_at is None


async def test_turn_unique_transcript_id_per_session(db: AsyncSession) -> None:
    profile = UserProfile(name="p", is_default=True)
    db.add(profile)
    await db.flush()
    call = CommunicationSession(
        profile_id=profile.id,
        channel=CommunicationChannel.CALL,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        remote_address="",
        remote_raw="",
        phonegate_event_id_start=1,
        started_at=datetime.now(UTC),
    )
    db.add(call)
    await db.flush()
    db.add(CommunicationTurn(session_id=call.id, phonegate_transcript_id=7, seq=1,
                             speaker=TurnSpeaker.EMPLOYER, text="a", occurred_at=datetime.now(UTC)))
    await db.flush()
    db.add(CommunicationTurn(session_id=call.id, phonegate_transcript_id=7, seq=2,
                             speaker=TurnSpeaker.EMPLOYER, text="b", occurred_at=datetime.now(UTC)))
    with pytest.raises(Exception):
        await db.flush()
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_phone_entities.py -q`
Expected: FAIL — `ImportError: CommunicationSession`.

- [ ] **Step 3: Implement**

Append to `app/models/entities.py` (imports: add the new enums to the existing
`from app.models.enums import (...)` block). Follow the file's existing style —
`enum_column(...)` for enum columns, `Mapped[...]` annotations, explicit
`created_at`/`updated_at` where `TimestampMixin` is not used.

```python
class CommunicationSession(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "communication_sessions"
    __table_args__ = (
        Index("ix_communication_sessions_profile_started", "profile_id", "started_at"),
        Index("ix_communication_sessions_remote_started", "remote_address", "started_at"),
        Index("ix_communication_sessions_ended_at", "ended_at"),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    application_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL")
    )
    canonical_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_jobs.id", ondelete="SET NULL")
    )
    source_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_jobs.id", ondelete="SET NULL")
    )
    contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("employer_contacts.id", ondelete="SET NULL")
    )
    channel: Mapped[CommunicationChannel] = mapped_column(
        enum_column(CommunicationChannel), nullable=False
    )
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[CommunicationDirection] = mapped_column(
        enum_column(CommunicationDirection), nullable=False
    )
    remote_address: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    remote_raw: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    phonegate_event_id_start: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ringing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[CommunicationOutcome | None] = mapped_column(
        enum_column(CommunicationOutcome)
    )
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rx_frame_stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CommunicationTurn(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "communication_turns"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "phonegate_transcript_id",
            name="uq_communication_turns_session_transcript",
        ),
    )

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("communication_sessions.id", ondelete="CASCADE"), index=True
    )
    phonegate_transcript_id: Mapped[int | None] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[TurnSpeaker] = mapped_column(enum_column(TurnSpeaker), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text)
    asr_backend: Mapped[str | None] = mapped_column(String(32))
    asr_confidence: Mapped[float | None] = mapped_column(Float)
    asr_meta: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class CallFact(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "call_facts"

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("communication_sessions.id", ondelete="CASCADE"), index=True
    )
    source_turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("communication_turns.id", ondelete="SET NULL")
    )
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_expression: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str | None] = mapped_column(String(500))
    asr_confidence: Mapped[float | None] = mapped_column(Float)
    llm_confidence: Mapped[float | None] = mapped_column(Float)
    state: Mapped[CallFactState] = mapped_column(enum_column(CallFactState), nullable=False)
    confirmed_by_turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("communication_turns.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class InterviewAppointment(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "interview_appointments"

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    application_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL")
    )
    communication_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("communication_sessions.id", ondelete="SET NULL")
    )
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Chisinau", nullable=False)
    format: Mapped[InterviewFormat] = mapped_column(
        enum_column(InterviewFormat), default=InterviewFormat.UNKNOWN, nullable=False
    )
    address: Mapped[str | None] = mapped_column(String(500))
    meeting_url: Mapped[str | None] = mapped_column(String(2048))
    contact_person: Mapped[str | None] = mapped_column(String(255))
    preparation: Mapped[str | None] = mapped_column(Text)
    status: Mapped[InterviewStatus] = mapped_column(
        enum_column(InterviewStatus), default=InterviewStatus.PROPOSED, nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class PhoneChannelHealth(Base):
    __tablename__ = "phone_channel_health"

    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[PhoneComponentStatus] = mapped_column(
        enum_column(PhoneComponentStatus), nullable=False
    )
    detail: Mapped[str | None] = mapped_column(String(500))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

Check that `Float` and `Integer` are in the existing `from sqlalchemy import (...)`
block at the top of `entities.py` (they are); add nothing else.

- [ ] **Step 4: Run the test, verify it passes**

Run: `uv run pytest tests/unit/test_phone_entities.py -q` → PASS.
Run: `uv run mypy app` → clean.

- [ ] **Step 5: Commit**

```bash
git add app/models/entities.py tests/unit/test_phone_entities.py
git commit -m "feat: add communication session/turn/fact/appointment/health entities"
```

---

## Task 3: Database migration

**Files:**
- Create: `migrations/versions/<generated>_phone_call_agent_phase_1.py`
- Test: `tests/integration/test_sqlite_migrations.py` (already exists; verify it
  still passes) + `tests/unit/test_phone_migration.py` (create)

**Interfaces:**
- Consumes: models from Task 2. Migration head before this is `5191960d5cc9`.
- Produces: five tables in the live schema; `down_revision` chain extended.

- [ ] **Step 1: Autogenerate the migration**

Run:
```bash
uv run alembic revision --autogenerate -m "phone call agent phase 1"
```
This writes a new file under `migrations/versions/`. Open it.

- [ ] **Step 2: Verify and tidy the generated file**

- `down_revision` must be `"5191960d5cc9"`.
- It must contain exactly five `op.create_table(...)` calls
  (`communication_sessions`, `communication_turns`, `call_facts`,
  `interview_appointments`, `phone_channel_health`) plus their indexes and the
  `uq_communication_turns_session_transcript` unique constraint.
- It must **not** touch any existing table (no `employer_contacts` alter — the
  `ContactType` enum is `native_enum=False` with no CHECK constraint, so
  `ContactType.PHONE` needs no DDL).
- Remove any stray `op.alter_column` / autogen noise on unrelated tables. If the
  autogen produced changes to tables other than the five new ones, delete those
  lines and re-run `alembic check` (Step 4) to confirm they were spurious.
- Match the docstring style of `migrations/versions/c7d4e6f8a912_review_learning_feedback.py`
  (Revision ID / Revises header only).

- [ ] **Step 3: Apply the migration**

Run: `uv run alembic upgrade head`
Expected: completes without error.

- [ ] **Step 4: Verify `alembic check` is clean**

Run: `uv run alembic check`
Expected: `No new upgrade operations detected.`
If it reports operations, the model and migration disagree — adjust the
migration (usual culprits: enum value lists, index names, `nullable`,
`server_default`) until clean.

- [ ] **Step 5: Write the migration smoke test**

Create `tests/unit/test_phone_migration.py`:

```python
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

import pytest


@pytest.mark.asyncio
async def test_phone_tables_present_after_metadata_create(sqlite_engine: AsyncEngine) -> None:
    def _tables(sync_conn: object) -> set[str]:
        return set(inspect(sync_conn).get_table_names())

    async with sqlite_engine.connect() as conn:
        names = await conn.run_sync(_tables)

    assert {
        "communication_sessions",
        "communication_turns",
        "call_facts",
        "interview_appointments",
        "phone_channel_health",
    } <= names
```

- [ ] **Step 6: Run the tests**

Run:
```bash
uv run pytest tests/unit/test_phone_migration.py tests/integration/test_sqlite_migrations.py -q
```
Expected: PASS (the second is service-independent; it runs migrations against
SQLite).

- [ ] **Step 7: Commit**

```bash
git add migrations/versions/ tests/unit/test_phone_migration.py
git commit -m "feat: migration for phone call agent phase 1 tables"
```

---

## Task 4: Settings block

**Files:**
- Modify: `app/settings/config.py`
- Modify: `.env.example`
- Test: `tests/unit/test_phone_settings.py` (create)

**Interfaces:**
- Produces on `Settings`: `phonegate_url: str`, `phonegate_auth_token:
  SecretStr | None`, `phone_agent_enabled: bool`,
  `phone_poll_idle_seconds: float`, `phone_poll_active_seconds: float`,
  `phone_http_timeout_seconds: float`, `phone_caller_region: str`,
  `phone_health_stale_after_seconds: int`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_settings.py`:

```python
from __future__ import annotations

import pytest

from app.settings.config import Settings


def test_phone_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.phone_agent_enabled is False
    assert settings.phonegate_url == "http://127.0.0.1:8888"
    assert settings.phonegate_auth_token is None
    assert settings.phone_poll_idle_seconds == 1.0
    assert settings.phone_caller_region == "MD"


def test_empty_token_is_unset() -> None:
    settings = Settings(_env_file=None, phonegate_auth_token="   ")
    assert settings.phonegate_auth_token is None


def test_production_requires_token_when_agent_enabled() -> None:
    base = dict(
        _env_file=None,
        environment="production",
        secret_key="x" * 40,
        public_base_url="https://jobs.example.com",
        database_url="postgresql+asyncpg://job_agent:real-pass@db/job_agent",
        admin_password_hash="$argon2id$dummy",
        llm_provider="openai",
        openai_api_key="sk-test",
        openai_model="gpt-x",
        phone_agent_enabled=True,
    )
    with pytest.raises(ValueError, match="PHONEGATE_AUTH_TOKEN"):
        Settings(**base)

    Settings(**base, phonegate_auth_token="a-real-token")  # no raise
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_phone_settings.py -q`
Expected: FAIL — attributes missing.

- [ ] **Step 3: Implement**

In `app/settings/config.py`, add after the `matching_*` fields:

```python
    phonegate_url: str = "http://127.0.0.1:8888"
    phonegate_auth_token: SecretStr | None = None
    phone_agent_enabled: bool = False
    phone_poll_idle_seconds: float = Field(default=1.0, ge=0.1, le=10)
    phone_poll_active_seconds: float = Field(default=0.3, ge=0.05, le=5)
    phone_http_timeout_seconds: float = Field(default=10.0, ge=1, le=30)
    phone_caller_region: str = "MD"
    phone_health_stale_after_seconds: int = Field(default=90, ge=10, le=3600)
```

Add `"phonegate_auth_token"` to the `@field_validator(...)` list that calls
`empty_secret_is_unset`.

In `validate_secure_production`, before the final `return self`, add:

```python
        if self.phone_agent_enabled and self.phonegate_auth_token is None:
            raise ValueError("PHONEGATE_AUTH_TOKEN is required when PHONE_AGENT_ENABLED is true")
```

In `.env.example`, add a commented block:

```dotenv
# Phone call agent (Phase 1: read-only observer). Off by default.
# PHONE_AGENT_ENABLED=false
# PHONEGATE_URL=https://phonegate.46-225-103-75.sslip.io
# PHONEGATE_AUTH_TOKEN=
# PHONE_CALLER_REGION=MD
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_phone_settings.py tests/unit/test_settings*.py -q`
Expected: PASS. Also `uv run mypy app` clean.

- [ ] **Step 5: Commit**

```bash
git add app/settings/config.py .env.example tests/unit/test_phone_settings.py
git commit -m "feat: phone call agent settings block"
```

---

## Task 5: Shared E.164 helper

**Files:**
- Create: `app/phone/__init__.py`, `app/phone/numbers.py`
- Modify: `app/crawlers/adapters/rabota_md/adapter.py` (reuse the helper)
- Test: `tests/unit/test_phone_numbers.py` (create)

**Interfaces:**
- Produces: `app.phone.numbers.normalize_e164(value: str | None, *, region: str =
  "MD") -> str | None` and `app.phone.numbers.mask_phone(value: str) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_numbers.py`:

```python
from __future__ import annotations

import pytest

from app.phone.numbers import mask_phone, normalize_e164


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+373 60 111 222", "+37360111222"),
        ("tel:+37360111222", "+37360111222"),
        ("060111222", "+37360111222"),
        ("00373 60 111 222", "+37360111222"),
        ("  +37360111222  ", "+37360111222"),
        ("", None),
        (None, None),
        ("not a phone", None),
        ("12", None),
    ],
)
def test_normalize_e164(raw: str | None, expected: str | None) -> None:
    assert normalize_e164(raw, region="MD") == expected


def test_normalize_is_idempotent() -> None:
    once = normalize_e164("060111222", region="MD")
    assert once is not None
    assert normalize_e164(once, region="MD") == once


def test_mask_phone_keeps_country_and_tail() -> None:
    assert mask_phone("+37360111222") == "+373••••222"
    assert mask_phone("") == "(withheld)"
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_phone_numbers.py -q`
Expected: FAIL — `ModuleNotFoundError: app.phone`.

- [ ] **Step 3: Implement**

Create `app/phone/__init__.py` (empty).

Create `app/phone/numbers.py`:

```python
from __future__ import annotations

import re

import phonenumbers

_TEL_PREFIX = re.compile(r"^tel:", re.IGNORECASE)


def normalize_e164(value: str | None, *, region: str = "MD") -> str | None:
    """Return an E.164 number, or ``None`` when the input is not a valid number.

    Idempotent on already-normalized input. Accepts a leading ``tel:``,
    international ``00`` prefixes, local Moldovan forms, and free formatting.
    """
    if not value:
        return None
    raw = _TEL_PREFIX.sub("", value.strip())
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    parse_region: str | None
    if raw.startswith("+"):
        candidate, parse_region = f"+{digits}", None
    elif digits.startswith("00"):
        candidate, parse_region = f"+{digits[2:]}", None
    else:
        candidate, parse_region = raw, region
    try:
        parsed = phonenumbers.parse(candidate, parse_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def mask_phone(value: str) -> str:
    """Mask a phone number for logs and audit: keep the country code and last 3."""
    if not value:
        return "(withheld)"
    if value.startswith("+") and len(value) > 7:
        head = value[:4]
        return f"{head}••••{value[-3:]}"
    return f"••••{value[-3:]}" if len(value) > 3 else "•••"
```

Then refactor `app/crawlers/adapters/rabota_md/adapter.py`: replace the body of
the private `_normalize_phone` static method so it delegates:

```python
    @staticmethod
    def _normalize_phone(value: str) -> str | None:
        from app.phone.numbers import normalize_e164

        return normalize_e164(value, region="MD")
```

Keep the `_SOURCE_SERVICE_PHONES` filtering in the caller unchanged.

- [ ] **Step 4: Run the tests**

Run:
```bash
uv run pytest tests/unit/test_phone_numbers.py tests/unit/test_rabota_adapter.py \
  tests/unit/test_rabota_parser_matrix.py -q
```
Expected: PASS (the rabota tests still pass with the delegated helper).
Run `uv run mypy app` → clean.

- [ ] **Step 5: Commit**

```bash
git add app/phone/ app/crawlers/adapters/rabota_md/adapter.py tests/unit/test_phone_numbers.py
git commit -m "feat: shared E.164 helper in app/phone/numbers"
```

---

## Task 6: Fake PhoneGate test fixture

**Files:**
- Create: `tests/fixtures/__init__.py` (if absent), `tests/fixtures/fake_phonegate.py`
- Test: `tests/unit/test_fake_phonegate.py` (create)

**Interfaces:**
- Produces: `tests.fixtures.fake_phonegate.FakePhoneGate` with:
  - `app: Starlette` ASGI application
  - `transport() -> httpx.ASGITransport`
  - `ring(caller: str) -> None`
  - `answer() -> None`
  - `transcript(*, speaker: str, text: str, backend: str = "groq",
    confidence: float | None = 0.9, meta: str = "") -> int`
  - `hangup() -> None`
  - `set_connected(value: bool) -> None`
  - `set_mode(value: str) -> None` (e.g. `"Zero-ADB"` / `"ADB fallback"`)
  - `restart() -> None` (resets event and transcript ids to 1, keeps call state)
  - routes: `GET /api/health`, `GET /api/device/status`,
    `GET /api/events`, `GET /api/call/transcript`
  - all routes require `Authorization: Bearer <anything>`; 401 otherwise.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_fake_phonegate.py`:

```python
from __future__ import annotations

import httpx
import pytest

from tests.fixtures.fake_phonegate import FakePhoneGate


@pytest.mark.asyncio
async def test_scripted_call_produces_ordered_events() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    fake.answer()
    tid = fake.transcript(speaker="rx", text="Здравствуйте")
    fake.hangup()

    async with httpx.AsyncClient(
        transport=fake.transport(), base_url="http://phonegate",
        headers={"Authorization": "Bearer test"},
    ) as client:
        status = (await client.get("/api/device/status")).json()
        assert status["call_state"] == "IDLE"
        assert status["latest_event_id"] >= 4

        events = (await client.get("/api/events", params={"after_id": 0})).json()
        types = [e["type"] for e in events["events"]]
        assert types[0] == "incoming_call"
        assert "transcript" in types
        assert events["events"] == sorted(events["events"], key=lambda e: e["id"])

    assert tid == 1


@pytest.mark.asyncio
async def test_requires_bearer() -> None:
    fake = FakePhoneGate()
    async with httpx.AsyncClient(transport=fake.transport(), base_url="http://phonegate") as client:
        assert (await client.get("/api/device/status")).status_code == 401


@pytest.mark.asyncio
async def test_restart_resets_event_ids() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    fake.restart()
    fake.transcript(speaker="rx", text="after restart")
    async with httpx.AsyncClient(
        transport=fake.transport(), base_url="http://phonegate",
        headers={"Authorization": "Bearer t"},
    ) as client:
        events = (await client.get("/api/events", params={"after_id": 0})).json()
        assert events["latest_id"] == 1
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_fake_phonegate.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `tests/fixtures/__init__.py` if it does not exist (empty).

Create `tests/fixtures/fake_phonegate.py`. It mirrors PhoneGate's real payloads
(`app/web/server.py` in the PhoneGate repo). Use Starlette directly:

```python
from __future__ import annotations

import time
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_CALL_STATES = {"IDLE", "RINGING", "IN_CALL"}


class FakePhoneGate:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._transcripts: list[dict[str, Any]] = []
        self._next_event_id = 1
        self._next_transcript_id = 1
        self._call_state = "IDLE"
        self._caller = ""
        self._connected = True
        self._mode = "Zero-ADB"
        self._daemon_version = "0.2.30"
        self._rx_stats = {"captured_frames": 0, "queued_frames": 0, "dropped_frames": 0}
        self.app = Starlette(routes=[
            Route("/api/health", self._health),
            Route("/api/device/status", self._status),
            Route("/api/events", self._events_route),
            Route("/api/call/transcript", self._transcript_route),
        ])

    # ---- scripting API -------------------------------------------------
    def _emit(self, event_type: str, data: dict[str, Any]) -> int:
        event = {"id": self._next_event_id, "type": event_type,
                 "timestamp": int(time.time() * 1000), "data": data}
        self._next_event_id += 1
        self._events.append(event)
        return event["id"]

    def _call_state_data(self) -> dict[str, Any]:
        return {"state": self._call_state, "duration": "00:00", "caller_number": self._caller,
                "caller_name": ""}

    def ring(self, caller: str) -> None:
        self._call_state, self._caller = "RINGING", caller
        self._emit("call_state", self._call_state_data())
        self._emit("incoming_call", self._call_state_data())

    def answer(self) -> None:
        self._call_state = "IN_CALL"
        self._emit("call_state", self._call_state_data())

    def transcript(self, *, speaker: str, text: str, backend: str = "groq",
                   confidence: float | None = 0.9, meta: str = "") -> int:
        record = {"id": self._next_transcript_id, "speaker": speaker, "text": text,
                  "meta": meta, "backend": backend, "confidence": confidence,
                  "timestamp": "00:00:00", "timestamp_ms": int(time.time() * 1000)}
        self._next_transcript_id += 1
        self._transcripts.append(record)
        self._emit("transcript", {"transcript": record})
        return record["id"]

    def hangup(self) -> None:
        self._call_state, self._caller = "IDLE", ""
        self._emit("call_state", self._call_state_data())

    def set_connected(self, value: bool) -> None:
        self._connected = value

    def set_mode(self, value: str) -> None:
        self._mode = value

    def restart(self) -> None:
        self._events.clear()
        self._transcripts.clear()
        self._next_event_id = 1
        self._next_transcript_id = 1

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app)

    # ---- ASGI routes -------------------------------------------------
    @staticmethod
    def _auth_ok(request: Request) -> bool:
        return request.headers.get("authorization", "").startswith("Bearer ")

    async def _health(self, request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def _status(self, request: Request) -> JSONResponse:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "auth"}, status_code=401)
        return JSONResponse({
            "connected": self._connected,
            "mode": self._mode if self._connected else "Ожидание daemon",
            "local_asr_enabled": False,
            "device": {"device_name": "A14", "battery": 87, "operator": "Orange",
                       "sim_operator": "Orange"},
            "daemon_version": self._daemon_version,
            "rx_audio_stats": dict(self._rx_stats),
            "call_state": self._call_state,
            "call_duration_seconds": 0,
            "caller_number": self._caller,
            "caller_name": "",
            "tx_active": False,
            "tx_preparing": False,
            "transcript_count": len(self._transcripts),
            "latest_event_id": self._next_event_id - 1,
        })

    async def _events_route(self, request: Request) -> JSONResponse:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "auth"}, status_code=401)
        after_id = int(request.query_params.get("after_id", "0"))
        limit = int(request.query_params.get("limit", "100"))
        event_type = request.query_params.get("event_type")
        rows = [e for e in self._events
                if e["id"] > after_id and (event_type is None or e["type"] == event_type)]
        last_incoming = next((e for e in reversed(self._events) if e["type"] == "incoming_call"), None)
        return JSONResponse({"events": rows[:limit], "count": len(rows[:limit]),
                             "latest_id": self._next_event_id - 1,
                             "last_incoming_call": last_incoming})

    async def _transcript_route(self, request: Request) -> JSONResponse:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "auth"}, status_code=401)
        after_id = int(request.query_params.get("after_id", "0"))
        rows = [t for t in self._transcripts if t["id"] > after_id]
        return JSONResponse({"entries": rows, "count": len(rows),
                             "latest_id": self._next_transcript_id - 1,
                             "call_state": self._call_state, "caller_number": self._caller,
                             "rx_audio_stats": dict(self._rx_stats)})
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_fake_phonegate.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/ tests/unit/test_fake_phonegate.py
git commit -m "test: scriptable fake PhoneGate fixture"
```

---

## Task 7: PhoneGate response schemas + telephony state

**Files:**
- Create: `app/phone/schemas.py`, `app/phone/states.py`
- Test: `tests/unit/test_phone_schemas.py` (create)

**Interfaces:**
- Produces `app.phone.schemas`:
  - `RxAudioStats(BaseModel)`: `captured_frames`, `queued_frames`,
    `dropped_frames` — all `int`, default `0`.
  - `DeviceStatus(BaseModel)`: `connected: bool`, `mode: str`, `call_state: str`,
    `caller_number: str = ""`, `caller_name: str = ""`, `daemon_version: str = ""`,
    `rx_audio_stats: RxAudioStats`, `device: dict[str, Any]`,
    `latest_event_id: int = 0`; property `is_daemon_mode -> bool` (True when
    `connected and mode == "Zero-ADB"`).
  - `PhoneEvent(BaseModel)`: `id: int`, `type: str`, `timestamp: int = 0`,
    `data: dict[str, Any]`.
  - `EventsPage(BaseModel)`: `events: list[PhoneEvent]`, `latest_id: int`,
    `last_incoming_call: dict[str, Any] | None = None`.
  - `TranscriptEntry(BaseModel)`: `id: int`, `speaker: str`, `text: str`,
    `meta: str = ""`, `backend: str = ""`, `confidence: float | None = None`,
    `timestamp_ms: int = 0`.
  - `TranscriptPage(BaseModel)`: `entries: list[TranscriptEntry]`,
    `latest_id: int`, `call_state: str = "IDLE"`, `caller_number: str = ""`.
  - All models use `model_config = ConfigDict(extra="ignore")`.
- Produces `app.phone.states`:
  - `TelephonyState(StrEnum)`: `IDLE`, `RINGING`, `CONNECTED`, `ENDED`.
  - `telephony_state_from_call_state(value: str) -> TelephonyState`
    (`"RINGING"->RINGING`, `"IN_CALL"->CONNECTED`, else `IDLE`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_schemas.py`:

```python
from __future__ import annotations

from app.phone.schemas import DeviceStatus, EventsPage, TranscriptPage
from app.phone.states import TelephonyState, telephony_state_from_call_state


def test_device_status_parses_and_ignores_extra() -> None:
    status = DeviceStatus.model_validate({
        "connected": True, "mode": "Zero-ADB", "call_state": "RINGING",
        "caller_number": "+37360111222", "rx_audio_stats": {"dropped_frames": 2},
        "device": {"battery": 87}, "latest_event_id": 12, "unknown_field": "x",
    })
    assert status.is_daemon_mode is True
    assert status.rx_audio_stats.dropped_frames == 2
    assert status.rx_audio_stats.captured_frames == 0


def test_device_status_adb_fallback_not_daemon_mode() -> None:
    status = DeviceStatus.model_validate(
        {"connected": True, "mode": "ADB fallback", "call_state": "IDLE",
         "rx_audio_stats": {}, "device": {}}
    )
    assert status.is_daemon_mode is False


def test_events_page_and_transcript_page() -> None:
    page = EventsPage.model_validate({
        "events": [{"id": 3, "type": "transcript", "data": {"transcript": {"id": 1}}}],
        "latest_id": 3, "last_incoming_call": None,
    })
    assert page.events[0].id == 3
    tp = TranscriptPage.model_validate({"entries": [], "latest_id": 0})
    assert tp.call_state == "IDLE"


def test_telephony_state_mapping() -> None:
    assert telephony_state_from_call_state("IN_CALL") is TelephonyState.CONNECTED
    assert telephony_state_from_call_state("RINGING") is TelephonyState.RINGING
    assert telephony_state_from_call_state("IDLE") is TelephonyState.IDLE
    assert telephony_state_from_call_state("weird") is TelephonyState.IDLE
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_phone_schemas.py -q` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

Create `app/phone/states.py`:

```python
from __future__ import annotations

from enum import StrEnum


class TelephonyState(StrEnum):
    IDLE = "idle"
    RINGING = "ringing"
    CONNECTED = "connected"
    ENDED = "ended"


def telephony_state_from_call_state(value: str) -> TelephonyState:
    return {
        "RINGING": TelephonyState.RINGING,
        "IN_CALL": TelephonyState.CONNECTED,
    }.get(value, TelephonyState.IDLE)
```

Create `app/phone/schemas.py`:

```python
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RxAudioStats(_Lenient):
    captured_frames: int = 0
    queued_frames: int = 0
    dropped_frames: int = 0


class DeviceStatus(_Lenient):
    connected: bool = False
    mode: str = ""
    call_state: str = "IDLE"
    caller_number: str = ""
    caller_name: str = ""
    daemon_version: str = ""
    rx_audio_stats: RxAudioStats = RxAudioStats()
    device: dict[str, Any] = {}
    latest_event_id: int = 0

    @property
    def is_daemon_mode(self) -> bool:
        return self.connected and self.mode == "Zero-ADB"


class PhoneEvent(_Lenient):
    id: int
    type: str
    timestamp: int = 0
    data: dict[str, Any] = {}


class EventsPage(_Lenient):
    events: list[PhoneEvent] = []
    latest_id: int = 0
    last_incoming_call: dict[str, Any] | None = None


class TranscriptEntry(_Lenient):
    id: int
    speaker: str
    text: str
    meta: str = ""
    backend: str = ""
    confidence: float | None = None
    timestamp_ms: int = 0


class TranscriptPage(_Lenient):
    entries: list[TranscriptEntry] = []
    latest_id: int = 0
    call_state: str = "IDLE"
    caller_number: str = ""
```

(Mutable defaults like `device: dict = {}` are acceptable here because pydantic
deep-copies model field defaults per instance.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_phone_schemas.py -q` → PASS.
`uv run mypy app` → clean.

- [ ] **Step 5: Commit**

```bash
git add app/phone/schemas.py app/phone/states.py tests/unit/test_phone_schemas.py
git commit -m "feat: PhoneGate response schemas and telephony state"
```

---

## Task 8: PhoneGateClient

**Files:**
- Create: `app/phone/client.py`
- Test: `tests/unit/test_phone_client.py` (create)

**Interfaces:**
- Consumes: `app.phone.schemas`; `FakePhoneGate` (test only).
- Produces `app.phone.client`:
  - `PhoneGateError(RuntimeError)` — HTTP 4xx.
  - `PhoneGateUnavailable(RuntimeError)` — transport error or HTTP 5xx.
  - `PhoneGateClient`:
    - `__init__(self, *, base_url: str, token: str, timeout: float = 10.0,
      transport: httpx.AsyncBaseTransport | None = None)`
    - `async aclose() -> None`; also an async context manager.
    - `async health() -> dict[str, Any]`
    - `async device_status() -> DeviceStatus`
    - `async events(self, *, after_id: int, limit: int = 250) -> EventsPage`
    - `async transcript(self, *, after_id: int = 0, limit: int = 250) -> TranscriptPage`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_client.py`:

```python
from __future__ import annotations

import httpx
import pytest

from app.phone.client import PhoneGateClient, PhoneGateError, PhoneGateUnavailable
from tests.fixtures.fake_phonegate import FakePhoneGate


@pytest.mark.asyncio
async def test_client_reads_status_and_events() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(
        base_url="http://phonegate", token="test", transport=fake.transport()
    ) as client:
        status = await client.device_status()
        assert status.call_state == "RINGING"
        page = await client.events(after_id=0)
        assert page.events[0].type == "call_state"


@pytest.mark.asyncio
async def test_client_maps_errors() -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/device/status":
            return httpx.Response(503, json={"detail": "down"})
        return httpx.Response(404, json={"detail": "nope"})

    transport = httpx.MockTransport(_handler)
    async with PhoneGateClient(
        base_url="http://phonegate", token="t", transport=transport
    ) as client:
        with pytest.raises(PhoneGateUnavailable):
            await client.device_status()
        with pytest.raises(PhoneGateError):
            await client.events(after_id=0)


@pytest.mark.asyncio
async def test_client_maps_transport_failure() -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with PhoneGateClient(
        base_url="http://phonegate", token="t", transport=httpx.MockTransport(_boom)
    ) as client:
        with pytest.raises(PhoneGateUnavailable):
            await client.health()
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_phone_client.py -q` → FAIL.

- [ ] **Step 3: Implement**

Create `app/phone/client.py`:

```python
from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from app.phone.schemas import DeviceStatus, EventsPage, TranscriptPage


class PhoneGateError(RuntimeError):
    """PhoneGate rejected the request (HTTP 4xx)."""


class PhoneGateUnavailable(RuntimeError):
    """PhoneGate could not be reached or returned a server error."""


class PhoneGateClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> PhoneGateClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.get(path, params=params)
        except httpx.RequestError as exc:
            raise PhoneGateUnavailable(f"{path}: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise PhoneGateUnavailable(f"{path}: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise PhoneGateError(f"{path}: HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise PhoneGateError(f"{path}: unexpected payload")
        return payload

    async def health(self) -> dict[str, Any]:
        return await self._get("/api/health")

    async def device_status(self) -> DeviceStatus:
        return DeviceStatus.model_validate(await self._get("/api/device/status"))

    async def events(self, *, after_id: int, limit: int = 250) -> EventsPage:
        return EventsPage.model_validate(
            await self._get("/api/events", {"after_id": after_id, "limit": limit})
        )

    async def transcript(self, *, after_id: int = 0, limit: int = 250) -> TranscriptPage:
        return TranscriptPage.model_validate(
            await self._get("/api/call/transcript", {"after_id": after_id, "limit": limit})
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_phone_client.py -q` → PASS.
`uv run mypy app` → clean. `uv run ruff check app/phone` → clean.

- [ ] **Step 5: Commit**

```bash
git add app/phone/client.py tests/unit/test_phone_client.py
git commit -m "feat: read-only PhoneGate REST client"
```

---

## Task 9: Caller correlation

**Files:**
- Create: `app/phone/correlation.py`
- Test: `tests/unit/test_phone_correlation.py` (create)

**Interfaces:**
- Consumes: `app.phone.numbers.normalize_e164`; models `EmployerContact`,
  `SourceJob`, `Application`, `UserProfile`; enums `ContactType`,
  `VerificationStatus`.
- Produces `app.phone.correlation`:
  - `@dataclass(frozen=True) CorrelationResult`: `profile_id: UUID`,
    `application_id: UUID | None`, `canonical_job_id: UUID | None`,
    `source_job_id: UUID | None`, `contact_id: UUID | None`.
  - `class CallerCorrelation`:
    - `__init__(self, *, region: str = "MD")`
    - `async resolve(self, session: AsyncSession, remote_raw: str) ->
      CorrelationResult | None` — returns `None` only when no `is_default`
      profile exists. May insert one `EmployerContact(PHONE)` row (flushed, not
      committed).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_correlation.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import (
    Application, CanonicalJob, EmployerContact, JobSource, SourceJob, UserProfile,
)
from app.models.enums import (
    ApplicationStatus, ContactType, JobStatus, VerificationStatus,
)
from app.phone.correlation import CallerCorrelation


@pytest_asyncio.fixture
async def db(sqlite_session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    async with sqlite_session_factory() as session:
        session.add(UserProfile(name="default", is_default=True))
        await session.commit()
        yield session


async def _job_with_phone(db: AsyncSession, phone: str) -> tuple[SourceJob, CanonicalJob]:
    src = JobSource(name="s", base_url="https://x", adapter_type="fixture_source")
    db.add(src)
    await db.flush()
    canonical = CanonicalJob(normalized_company="ACME", normalized_title="Loader",
                             canonical_fingerprint=uuid4().hex, status=JobStatus.ACTIVE)
    db.add(canonical)
    await db.flush()
    job = SourceJob(
        source_id=src.id, canonical_job_id=canonical.id, external_job_id=uuid4().hex,
        canonical_url="https://x/1", title="Loader", content_hash="h", matching_content_hash="m",
        source_fingerprint="f", public_phone=phone, status=JobStatus.ACTIVE,
    )
    db.add(job)
    await db.flush()
    return job, canonical


async def test_no_default_profile_returns_none(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        result = await CallerCorrelation().resolve(session, "+37360111222")
        assert result is None


async def test_unknown_number_falls_back_to_default_profile(db: AsyncSession) -> None:
    result = await CallerCorrelation().resolve(db, "+37360999888")
    assert result is not None
    assert result.application_id is None
    assert result.contact_id is None


async def test_matches_existing_phone_contact_and_application(db: AsyncSession) -> None:
    job, canonical = await _job_with_phone(db, "+37360111222")
    profile = (await db.scalars(UserProfile.__table__.select())).first()
    db.add(EmployerContact(
        canonical_job_id=canonical.id, source_job_id=job.id, value="+37360111222",
        contact_type=ContactType.PHONE, discovery_source="test",
        verification_status=VerificationStatus.UNVERIFIED, confidence=0.6,
        evidence_url="https://x/1",
    ))
    db.add(Application(
        profile_id=profile.id, canonical_job_id=canonical.id, source_job_id=job.id,
        resume_id=uuid4(), recipient_contact_id=uuid4(), subject="s", body="b",
        language="ru", status=ApplicationStatus.PENDING_REVIEW, idempotency_key=uuid4().hex,
    ))
    await db.flush()

    result = await CallerCorrelation().resolve(db, "+373 60 111 222")
    assert result is not None
    assert result.canonical_job_id == canonical.id
    assert result.application_id is not None


async def test_creates_phone_contact_from_source_job(db: AsyncSession) -> None:
    job, canonical = await _job_with_phone(db, "+37360111222")
    result = await CallerCorrelation().resolve(db, "+37360111222")
    assert result is not None
    assert result.contact_id is not None
    contacts = (await db.scalars(EmployerContact.__table__.select())).all()
    assert len(contacts) == 1
```

(Adjust the `Application` constructor kwargs to whatever `app/models/entities.py`
requires — check the model before writing; `resume_id` / `recipient_contact_id`
are `NOT NULL` FKs, so pass throwaway `uuid4()` values; the FK is only enforced
on commit and these tests flush.)

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_phone_correlation.py -q` → FAIL.

- [ ] **Step 3: Implement**

Create `app/phone/correlation.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Application, EmployerContact, SourceJob, UserProfile
from app.models.enums import ContactType, VerificationStatus
from app.phone.numbers import normalize_e164


@dataclass(frozen=True, slots=True)
class CorrelationResult:
    profile_id: UUID
    application_id: UUID | None
    canonical_job_id: UUID | None
    source_job_id: UUID | None
    contact_id: UUID | None


def _domain(value: str | None) -> str | None:
    if not value:
        return None
    return (urlsplit(value).hostname or "").lower() or None


class CallerCorrelation:
    def __init__(self, *, region: str = "MD") -> None:
        self._region = region

    async def resolve(
        self, session: AsyncSession, remote_raw: str
    ) -> CorrelationResult | None:
        default_profile_id = await session.scalar(
            select(UserProfile.id).where(UserProfile.is_default.is_(True)).limit(1)
        )
        if default_profile_id is None:
            return None

        e164 = normalize_e164(remote_raw, region=self._region)
        if e164 is None:
            return CorrelationResult(default_profile_id, None, None, None, None)

        contact = await session.scalar(
            select(EmployerContact)
            .where(
                EmployerContact.contact_type == ContactType.PHONE,
                EmployerContact.value == e164,
            )
            .order_by(EmployerContact.confidence.desc(), EmployerContact.created_at.desc())
            .limit(1)
        )
        job: SourceJob | None = None
        if contact is None:
            job = await session.scalar(
                select(SourceJob)
                .where(SourceJob.public_phone == e164)
                .order_by(SourceJob.last_seen_at.desc())
                .limit(1)
            )
            if job is not None and job.canonical_job_id is not None:
                contact = EmployerContact(
                    canonical_job_id=job.canonical_job_id,
                    source_job_id=job.id,
                    value=e164,
                    contact_type=ContactType.PHONE,
                    discovery_source="inbound_call_match_public_phone",
                    official_domain=_domain(job.employer_url or job.canonical_url),
                    verification_status=VerificationStatus.UNVERIFIED,
                    confidence=0.6,
                    evidence_url=job.canonical_url,
                )
                session.add(contact)
                await session.flush()

        if contact is None:
            return CorrelationResult(default_profile_id, None, None, None, None)

        application = await session.scalar(
            select(Application)
            .where(Application.canonical_job_id == contact.canonical_job_id)
            .order_by(Application.created_at.desc())
            .limit(1)
        )
        profile_id = application.profile_id if application is not None else default_profile_id
        return CorrelationResult(
            profile_id=profile_id,
            application_id=application.id if application is not None else None,
            canonical_job_id=contact.canonical_job_id,
            source_job_id=contact.source_job_id,
            contact_id=contact.id,
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_phone_correlation.py -q` → PASS.
`uv run mypy app` → clean.

- [ ] **Step 5: Commit**

```bash
git add app/phone/correlation.py tests/unit/test_phone_correlation.py
git commit -m "feat: inbound caller correlation to application/profile"
```

---

## Task 10: Session store — lifecycle

**Files:**
- Create: `app/phone/sessions.py`
- Test: `tests/unit/test_phone_sessions.py` (create)

**Interfaces:**
- Consumes: models `CommunicationSession`; enums `CommunicationChannel`,
  `CommunicationDirection`, `CommunicationOutcome`; `CorrelationResult` (Task 9);
  `app.database.base.utcnow`.
- Produces `app.phone.sessions.SessionStore`:
  - `async find_open(self, session: AsyncSession) -> CommunicationSession | None`
    — the single row with `ended_at IS NULL`, newest first.
  - `async open(self, session: AsyncSession, *, remote_raw: str,
    remote_address: str, event_id: int, correlation: CorrelationResult,
    opened_at: datetime, needs_review: bool = False, note: str | None = None)
    -> CommunicationSession`
  - `async touch_ringing(self, s: CommunicationSession, when: datetime) -> None`
  - `async touch_answered(self, s: CommunicationSession, when: datetime) -> None`
  - `async close(self, session: AsyncSession, s: CommunicationSession, *,
    outcome: CommunicationOutcome, ended_at: datetime, needs_review: bool = False,
    rx_stats: dict[str, Any] | None = None, note: str | None = None) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_sessions.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import UserProfile
from app.models.enums import CommunicationOutcome
from app.phone.correlation import CorrelationResult
from app.phone.sessions import SessionStore


@pytest_asyncio.fixture
async def db(sqlite_session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    async with sqlite_session_factory() as session:
        p = UserProfile(name="d", is_default=True)
        session.add(p)
        await session.commit()
        session.info["profile_id"] = p.id
        yield session


def _corr(profile_id: object) -> CorrelationResult:
    return CorrelationResult(profile_id, None, None, None, None)  # type: ignore[arg-type]


async def test_open_find_close(db: AsyncSession) -> None:
    store = SessionStore()
    now = datetime.now(UTC)
    call = await store.open(db, remote_raw="+37360111222", remote_address="+37360111222",
                            event_id=3, correlation=_corr(db.info["profile_id"]), opened_at=now)
    await db.commit()

    open_row = await store.find_open(db)
    assert open_row is not None and open_row.id == call.id

    await store.touch_answered(call, now)
    await store.close(db, call, outcome=CommunicationOutcome.COMPLETED, ended_at=now)
    await db.commit()

    assert await store.find_open(db) is None
    refreshed = await db.get(type(call), call.id)
    assert refreshed is not None and refreshed.outcome == CommunicationOutcome.COMPLETED
    assert refreshed.answered_at is not None
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_phone_sessions.py -q` → FAIL.

- [ ] **Step 3: Implement**

Create `app/phone/sessions.py`:

```python
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.models.entities import CommunicationSession
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
)
from app.phone.correlation import CorrelationResult


class SessionStore:
    async def find_open(self, session: AsyncSession) -> CommunicationSession | None:
        return await session.scalar(
            select(CommunicationSession)
            .where(CommunicationSession.ended_at.is_(None))
            .order_by(CommunicationSession.started_at.desc())
            .limit(1)
        )

    async def open(
        self,
        session: AsyncSession,
        *,
        remote_raw: str,
        remote_address: str,
        event_id: int,
        correlation: CorrelationResult,
        opened_at: datetime,
        needs_review: bool = False,
        note: str | None = None,
    ) -> CommunicationSession:
        call = CommunicationSession(
            profile_id=correlation.profile_id,
            application_id=correlation.application_id,
            canonical_job_id=correlation.canonical_job_id,
            source_job_id=correlation.source_job_id,
            contact_id=correlation.contact_id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address=remote_address,
            remote_raw=remote_raw,
            phonegate_event_id_start=event_id,
            started_at=opened_at,
            ringing_at=opened_at,
            needs_review=needs_review,
            diagnostics={"note": note} if note else {},
        )
        session.add(call)
        await session.flush()
        return call

    async def touch_ringing(self, call: CommunicationSession, when: datetime) -> None:
        if call.ringing_at is None:
            call.ringing_at = when

    async def touch_answered(self, call: CommunicationSession, when: datetime) -> None:
        if call.answered_at is None:
            call.answered_at = when

    async def close(
        self,
        session: AsyncSession,
        call: CommunicationSession,
        *,
        outcome: CommunicationOutcome,
        ended_at: datetime,
        needs_review: bool = False,
        rx_stats: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> None:
        call.ended_at = ended_at
        call.outcome = outcome
        if needs_review:
            call.needs_review = True
        if rx_stats is not None:
            call.rx_frame_stats = rx_stats
        if note:
            call.diagnostics = {**call.diagnostics, "close_note": note}
        call.updated_at = utcnow()
        await session.flush()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_phone_sessions.py -q` → PASS. `mypy` clean.

- [ ] **Step 5: Commit**

```bash
git add app/phone/sessions.py tests/unit/test_phone_sessions.py
git commit -m "feat: session store lifecycle for phone calls"
```

---

## Task 11: Session store — idempotent turn append

**Files:**
- Modify: `app/phone/sessions.py`
- Test: `tests/unit/test_phone_sessions.py` (extend)

**Interfaces:**
- Consumes: `app.phone.schemas.TranscriptEntry`; models `CommunicationTurn`;
  enum `TurnSpeaker`.
- Produces on `SessionStore`:
  - `async append_turn(self, session: AsyncSession, *, session_id: UUID,
    entry: TranscriptEntry) -> CommunicationTurn | None` — inserts one turn,
    returns `None` when `(session_id, entry.id)` already exists.
  - module function `speaker_from_phonegate(value: str) -> TurnSpeaker`
    (`"rx"->EMPLOYER`, `"tx"->OPERATOR`, else `SYSTEM`).

- [ ] **Step 1: Write the failing test (append to the file)**

```python
async def test_append_turn_is_idempotent(db: AsyncSession) -> None:
    from app.phone.schemas import TranscriptEntry
    from app.phone.sessions import SessionStore, speaker_from_phonegate
    from app.models.enums import TurnSpeaker

    store = SessionStore()
    now = datetime.now(UTC)
    call = await store.open(db, remote_raw="+3736011", remote_address="+3736011",
                            event_id=1, correlation=_corr(db.info["profile_id"]), opened_at=now)
    await db.flush()

    entry = TranscriptEntry(id=5, speaker="rx", text="Здравствуйте", confidence=0.8,
                            backend="groq", timestamp_ms=1)
    first = await store.append_turn(db, session_id=call.id, entry=entry)
    assert first is not None and first.seq == 1 and first.speaker == TurnSpeaker.EMPLOYER
    second = await store.append_turn(db, session_id=call.id, entry=entry)
    assert second is None

    entry2 = TranscriptEntry(id=6, speaker="tx", text="ответ", timestamp_ms=2)
    third = await store.append_turn(db, session_id=call.id, entry=entry2)
    assert third is not None and third.seq == 2 and third.speaker == TurnSpeaker.OPERATOR
    assert speaker_from_phonegate("weird") is TurnSpeaker.SYSTEM
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/unit/test_phone_sessions.py::test_append_turn_is_idempotent -q` → FAIL.

- [ ] **Step 3: Implement (add to `app/phone/sessions.py`)**

```python
from uuid import UUID  # add to imports

from sqlalchemy import func, select  # extend existing import

from app.models.entities import CommunicationSession, CommunicationTurn  # extend
from app.models.enums import (  # extend
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    TurnSpeaker,
)
from app.phone.schemas import TranscriptEntry  # add


def speaker_from_phonegate(value: str) -> TurnSpeaker:
    return {"rx": TurnSpeaker.EMPLOYER, "tx": TurnSpeaker.OPERATOR}.get(
        value, TurnSpeaker.SYSTEM
    )
```

Add the method to `SessionStore`:

```python
    async def append_turn(
        self,
        session: AsyncSession,
        *,
        session_id: UUID,
        entry: TranscriptEntry,
    ) -> CommunicationTurn | None:
        exists = await session.scalar(
            select(CommunicationTurn.id).where(
                CommunicationTurn.session_id == session_id,
                CommunicationTurn.phonegate_transcript_id == entry.id,
            )
        )
        if exists is not None:
            return None
        count = await session.scalar(
            select(func.count(CommunicationTurn.id)).where(
                CommunicationTurn.session_id == session_id
            )
        )
        from datetime import UTC, datetime

        turn = CommunicationTurn(
            session_id=session_id,
            phonegate_transcript_id=entry.id,
            seq=int(count or 0) + 1,
            speaker=speaker_from_phonegate(entry.speaker),
            text=entry.text,
            raw_text=entry.text,
            asr_backend=entry.backend or None,
            asr_confidence=entry.confidence,
            asr_meta=entry.meta or None,
            occurred_at=datetime.fromtimestamp(entry.timestamp_ms / 1000, tz=UTC)
            if entry.timestamp_ms
            else datetime.now(UTC),
        )
        session.add(turn)
        await session.flush()
        return turn
```

(Move the `from datetime import ...` to the top of the module instead of inline —
shown inline only to mark where it is used.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_phone_sessions.py -q` → PASS. `mypy` clean.

- [ ] **Step 5: Commit**

```bash
git add app/phone/sessions.py tests/unit/test_phone_sessions.py
git commit -m "feat: idempotent transcript turn append"
```

---

## Task 12: Health tracker

**Files:**
- Create: `app/phone/health.py`
- Test: `tests/unit/test_phone_health.py` (create)

**Interfaces:**
- Consumes: `app.phone.schemas.DeviceStatus`; model `PhoneChannelHealth`; enum
  `PhoneComponentStatus`; `app.database.base.utcnow`.
- Produces `app.phone.health`:
  - `@dataclass HealthComponent`: `component: str`,
    `status: PhoneComponentStatus`, `detail: str | None`,
    `last_ok_at: datetime | None`.
  - `class HealthTracker`:
    - `record_status(self, status: DeviceStatus) -> None`
    - `record_transport_error(self, name: str) -> None`
    - `mark_poll_ok(self) -> None`
    - `components(self) -> list[HealthComponent]`
    - `async persist(self, session: AsyncSession) -> None`
  - `channel_status(components: list[HealthComponent]) -> PhoneComponentStatus` —
    worst of `phonegate_transport`, `a14_daemon`, `gsm_line`, `agent`
    (`unavailable` > `degraded` > `unknown` > `healthy`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_health.py`:

```python
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import PhoneChannelHealth
from app.models.enums import PhoneComponentStatus
from app.phone.health import HealthTracker, channel_status
from app.phone.schemas import DeviceStatus


def _status(**kw: object) -> DeviceStatus:
    base = {"connected": True, "mode": "Zero-ADB", "call_state": "IDLE",
            "rx_audio_stats": {}, "device": {"sim_operator": "Orange"}}
    base.update(kw)
    return DeviceStatus.model_validate(base)


def test_healthy_when_daemon_connected() -> None:
    tracker = HealthTracker()
    tracker.mark_poll_ok()
    tracker.record_status(_status())
    by_name = {c.component: c for c in tracker.components()}
    assert by_name["phonegate_transport"].status is PhoneComponentStatus.HEALTHY
    assert by_name["a14_daemon"].status is PhoneComponentStatus.HEALTHY
    assert channel_status(tracker.components()) is PhoneComponentStatus.HEALTHY


def test_transport_error_is_unavailable() -> None:
    tracker = HealthTracker()
    tracker.record_transport_error("ConnectError")
    by_name = {c.component: c for c in tracker.components()}
    assert by_name["phonegate_transport"].status is PhoneComponentStatus.UNAVAILABLE
    assert channel_status(tracker.components()) is PhoneComponentStatus.UNAVAILABLE


def test_adb_fallback_is_degraded() -> None:
    tracker = HealthTracker()
    tracker.mark_poll_ok()
    tracker.record_status(_status(mode="ADB fallback"))
    by_name = {c.component: c for c in tracker.components()}
    assert by_name["a14_daemon"].status is PhoneComponentStatus.DEGRADED


@pytest.mark.asyncio
async def test_persist_upserts_rows(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tracker = HealthTracker()
    tracker.mark_poll_ok()
    tracker.record_status(_status())
    async with sqlite_session_factory() as session:
        await tracker.persist(session)
        await session.commit()
        await tracker.persist(session)  # second upsert, no duplicate PK
        await session.commit()
        rows = (await session.scalars(select(PhoneChannelHealth))).all()
    assert {r.component for r in rows} >= {"phonegate_transport", "a14_daemon", "agent"}
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_phone_health.py -q` → FAIL.

- [ ] **Step 3: Implement**

Create `app/phone/health.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.models.entities import PhoneChannelHealth
from app.models.enums import PhoneComponentStatus
from app.phone.schemas import DeviceStatus

_RANK = {
    PhoneComponentStatus.HEALTHY: 0,
    PhoneComponentStatus.UNKNOWN: 1,
    PhoneComponentStatus.DEGRADED: 2,
    PhoneComponentStatus.UNAVAILABLE: 3,
}
_CHANNEL_COMPONENTS = ("phonegate_transport", "a14_daemon", "gsm_line", "agent")


@dataclass(slots=True)
class HealthComponent:
    component: str
    status: PhoneComponentStatus
    detail: str | None = None
    last_ok_at: datetime | None = None


def channel_status(components: list[HealthComponent]) -> PhoneComponentStatus:
    relevant = [c for c in components if c.component in _CHANNEL_COMPONENTS]
    if not relevant:
        return PhoneComponentStatus.UNKNOWN
    return max(relevant, key=lambda c: _RANK[c.status]).status


class HealthTracker:
    def __init__(self) -> None:
        self._components: dict[str, HealthComponent] = {}

    def _set(self, name: str, status: PhoneComponentStatus, detail: str | None = None) -> None:
        prior = self._components.get(name)
        last_ok = prior.last_ok_at if prior else None
        if status is PhoneComponentStatus.HEALTHY:
            last_ok = utcnow()
        self._components[name] = HealthComponent(name, status, detail, last_ok)

    def mark_poll_ok(self) -> None:
        self._set("agent", PhoneComponentStatus.HEALTHY, "poll ok")

    def record_transport_error(self, name: str) -> None:
        self._set("phonegate_transport", PhoneComponentStatus.UNAVAILABLE, name)
        self._set("a14_daemon", PhoneComponentStatus.UNKNOWN, "no status")
        self._set("gsm_line", PhoneComponentStatus.UNKNOWN, "no status")

    def record_status(self, status: DeviceStatus) -> None:
        self._set("phonegate_transport", PhoneComponentStatus.HEALTHY, None)
        if status.is_daemon_mode:
            self._set("a14_daemon", PhoneComponentStatus.HEALTHY, "Zero-ADB")
        elif status.connected:
            self._set("a14_daemon", PhoneComponentStatus.DEGRADED, "ADB fallback")
        else:
            self._set("a14_daemon", PhoneComponentStatus.UNAVAILABLE, "not connected")
        self._set(
            "gsm_line",
            PhoneComponentStatus.HEALTHY if status.connected else PhoneComponentStatus.UNAVAILABLE,
            f"call_state={status.call_state}",
        )
        operator = str(status.device.get("sim_operator") or status.device.get("operator") or "")
        self._set(
            "sim_account",
            PhoneComponentStatus.HEALTHY if operator else PhoneComponentStatus.UNKNOWN,
            operator or "no telemetry",
        )

    def components(self) -> list[HealthComponent]:
        return list(self._components.values())

    async def persist(self, session: AsyncSession) -> None:
        dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        now = utcnow()
        for component in self._components.values():
            stmt = insert(PhoneChannelHealth).values(
                component=component.component,
                status=component.status,
                detail=component.detail,
                last_ok_at=component.last_ok_at,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[PhoneChannelHealth.component],
                set_={
                    "status": stmt.excluded.status,
                    "detail": stmt.excluded.detail,
                    "last_ok_at": stmt.excluded.last_ok_at,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_phone_health.py -q` → PASS. `mypy` clean.

- [ ] **Step 5: Commit**

```bash
git add app/phone/health.py tests/unit/test_phone_health.py
git commit -m "feat: phone channel health tracker"
```

---

## Task 13: Ingest loop — cursor, reconcile, resync

**Files:**
- Create: `app/phone/ingest.py`
- Test: `tests/unit/test_phone_ingest_cursor.py` (create)

**Interfaces:**
- Consumes: `PhoneGateClient`, `SessionStore`, `CallerCorrelation`,
  `HealthTracker`, `Settings`, an async Redis client
  (`redis.asyncio.Redis`).
- Produces `app.phone.ingest`:
  - `EVENTS_CURSOR_KEY = "job-agent:phone:events:cursor"`
  - `class IngestLoop`:
    - `__init__(self, *, client, session_factory, redis, correlation, health,
      settings)`
    - `self._cursor: int`, `self._open_session_id: UUID | None`
    - `async load_cursor(self) -> int`
    - `async save_cursor(self, value: int) -> None`
    - `async reconcile(self, status: DeviceStatus) -> None` — start-of-process
      and post-resync reconciliation (spec §7.5).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_ingest_cursor.py`:

```python
from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CommunicationSession, UserProfile
from app.models.enums import (
    CommunicationChannel, CommunicationDirection, CommunicationOutcome,
)
from app.phone.correlation import CallerCorrelation
from app.phone.health import HealthTracker
from app.phone.ingest import EVENTS_CURSOR_KEY, IngestLoop
from app.phone.schemas import DeviceStatus
from app.phone.sessions import SessionStore
from app.settings.config import Settings
from datetime import UTC, datetime


def _loop(factory: async_sessionmaker[AsyncSession], redis: object) -> IngestLoop:
    return IngestLoop(
        client=None,  # type: ignore[arg-type]  # not used by the methods under test
        session_factory=factory,
        redis=redis,
        correlation=CallerCorrelation(),
        health=HealthTracker(),
        settings=Settings(_env_file=None),
    )


@pytest_asyncio.fixture
async def redis() -> object:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.mark.asyncio
async def test_load_cursor_defaults_to_latest(
    sqlite_session_factory: async_sessionmaker[AsyncSession], redis: object
) -> None:
    loop = _loop(sqlite_session_factory, redis)
    assert await loop.load_cursor() == 0
    await loop.save_cursor(9)
    assert await redis.get(EVENTS_CURSOR_KEY) == "9"
    assert await loop.load_cursor() == 9


@pytest.mark.asyncio
async def test_reconcile_closes_dangling_open_session_when_idle(
    sqlite_session_factory: async_sessionmaker[AsyncSession], redis: object
) -> None:
    async with sqlite_session_factory() as session:
        p = UserProfile(name="d", is_default=True)
        session.add(p)
        await session.flush()
        session.add(CommunicationSession(
            profile_id=p.id, channel=CommunicationChannel.CALL, transport="phonegate",
            direction=CommunicationDirection.INBOUND, remote_address="", remote_raw="",
            phonegate_event_id_start=1, started_at=datetime.now(UTC),
        ))
        await session.commit()

    loop = _loop(sqlite_session_factory, redis)
    await loop.reconcile(DeviceStatus.model_validate(
        {"connected": True, "mode": "Zero-ADB", "call_state": "IDLE",
         "rx_audio_stats": {}, "device": {}}
    ))

    async with sqlite_session_factory() as session:
        row = await SessionStore().find_open(session)
    assert row is None
```

Note: `fakeredis` is not currently a dev dependency. Add it in Step 3.

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_phone_ingest_cursor.py -q` → FAIL
(`ModuleNotFoundError: fakeredis` then `app.phone.ingest`).

- [ ] **Step 3: Implement**

Add `fakeredis` to `[project.optional-dependencies].dev` in `pyproject.toml`
(`"fakeredis>=2.26,<3"`), then `uv sync --extra dev`.

Create `app/phone/ingest.py`:

```python
from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.base import utcnow
from app.models.enums import CommunicationOutcome
from app.phone.client import PhoneGateClient
from app.phone.correlation import CallerCorrelation
from app.phone.health import HealthTracker
from app.phone.schemas import DeviceStatus
from app.phone.sessions import SessionStore
from app.settings.config import Settings

logger = structlog.get_logger(__name__)

EVENTS_CURSOR_KEY = "job-agent:phone:events:cursor"


class IngestLoop:
    def __init__(
        self,
        *,
        client: PhoneGateClient,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        correlation: CallerCorrelation,
        health: HealthTracker,
        settings: Settings,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._redis = redis
        self._correlation = correlation
        self._health = health
        self._settings = settings
        self._store = SessionStore()
        self._cursor = 0
        self._open_session_id: UUID | None = None

    async def load_cursor(self) -> int:
        raw = await self._redis.get(EVENTS_CURSOR_KEY)
        try:
            self._cursor = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            self._cursor = 0
        return self._cursor

    async def save_cursor(self, value: int) -> None:
        self._cursor = value
        await self._redis.set(EVENTS_CURSOR_KEY, str(value))

    async def reconcile(self, status: DeviceStatus) -> None:
        """Bring local state in line with PhoneGate after a start or a resync."""
        async with self._session_factory() as session:
            open_row = await self._store.find_open(session)
            if open_row is None:
                self._open_session_id = None
            elif status.call_state == "IDLE":
                await self._store.close(
                    session,
                    open_row,
                    outcome=CommunicationOutcome.UNKNOWN,
                    ended_at=utcnow(),
                    needs_review=True,
                    note="reconcile_closed_no_active_call",
                )
                self._open_session_id = None
            else:
                self._open_session_id = open_row.id
                open_row.diagnostics = {
                    **open_row.diagnostics,
                    "reconciled_at": utcnow().isoformat(),
                }
            await session.commit()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_phone_ingest_cursor.py -q` → PASS. `mypy` clean.

- [ ] **Step 5: Commit**

```bash
git add app/phone/ingest.py pyproject.toml uv.lock tests/unit/test_phone_ingest_cursor.py
git commit -m "feat: ingest loop cursor and reconcile"
```

---

## Task 14: Ingest loop — event dispatch and poll cycle

**Files:**
- Modify: `app/phone/ingest.py`
- Test: `tests/unit/test_phone_ingest_dispatch.py` (create)

**Interfaces:**
- Consumes: everything from Task 13; `PhoneGateClient`;
  `app.audit.record_audit_event`; `app.phone.numbers.mask_phone`;
  `telephony_state_from_call_state`.
- Produces on `IngestLoop`:
  - `async run_cycle(self) -> None` — one poll iteration (spec §7.1): status →
    events (or `transcript` reconcile) → dispatch → cursor save → health persist.
  - `async run_forever(self, *, should_stop: Callable[[], bool]) -> None` —
    calls `reconcile` once, then `run_cycle` in a loop with adaptive sleep,
    stopping when `should_stop()` is true.
  - private `_dispatch(self, session, event, status)`,
    `_on_incoming_call`, `_on_call_state`, `_on_transcript`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_ingest_dispatch.py`:

```python
from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import CommunicationOutcome
from app.phone.client import PhoneGateClient
from app.phone.correlation import CallerCorrelation
from app.phone.health import HealthTracker
from app.phone.ingest import IngestLoop
from app.phone.sessions import SessionStore
from app.settings.config import Settings
from tests.fixtures.fake_phonegate import FakePhoneGate


@pytest_asyncio.fixture
async def redis() -> object:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def profiled_factory(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    async with sqlite_session_factory() as session:
        session.add(UserProfile(name="d", is_default=True))
        await session.commit()
    return sqlite_session_factory


async def _drain(loop: IngestLoop, cycles: int = 6) -> None:
    for _ in range(cycles):
        await loop.run_cycle()


@pytest.mark.asyncio
async def test_full_scripted_call_persists_session_and_turns(
    profiled_factory: async_sessionmaker[AsyncSession], redis: object
) -> None:
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = IngestLoop(
            client=client, session_factory=profiled_factory, redis=redis,
            correlation=CallerCorrelation(), health=HealthTracker(),
            settings=Settings(_env_file=None),
        )
        await loop.load_cursor()

        fake.ring("+37360111222")
        fake.answer()
        fake.transcript(speaker="rx", text="Здравствуйте, по вакансии")
        fake.transcript(speaker="rx", text="в четверг в два")
        fake.hangup()
        await _drain(loop)

    async with profiled_factory() as session:
        calls = (await session.scalars(select(CommunicationSession))).all()
        turns = (await session.scalars(select(CommunicationTurn))).all()
    assert len(calls) == 1
    assert calls[0].outcome == CommunicationOutcome.COMPLETED
    assert calls[0].answered_at is not None
    assert [t.text for t in sorted(turns, key=lambda t: t.seq)] == [
        "Здравствуйте, по вакансии", "в четверг в два",
    ]


@pytest.mark.asyncio
async def test_missed_call_outcome(
    profiled_factory: async_sessionmaker[AsyncSession], redis: object
) -> None:
    fake = FakePhoneGate()
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        loop = IngestLoop(client=client, session_factory=profiled_factory, redis=redis,
                          correlation=CallerCorrelation(), health=HealthTracker(),
                          settings=Settings(_env_file=None))
        await loop.load_cursor()
        fake.ring("+37360111222")
        fake.hangup()
        await _drain(loop)
    async with profiled_factory() as session:
        call = (await session.scalars(select(CommunicationSession))).one()
    assert call.outcome == CommunicationOutcome.MISSED
    assert call.answered_at is None


@pytest.mark.asyncio
async def test_reingest_is_idempotent(
    profiled_factory: async_sessionmaker[AsyncSession], redis: object
) -> None:
    fake = FakePhoneGate()
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        loop = IngestLoop(client=client, session_factory=profiled_factory, redis=redis,
                          correlation=CallerCorrelation(), health=HealthTracker(),
                          settings=Settings(_env_file=None))
        await loop.load_cursor()
        fake.ring("+37360111222"); fake.answer()
        fake.transcript(speaker="rx", text="a"); fake.hangup()
        await _drain(loop)
        await loop.save_cursor(0)   # replay every event
        await _drain(loop)
    async with profiled_factory() as session:
        turns = (await session.scalars(select(CommunicationTurn))).all()
        calls = (await session.scalars(select(CommunicationSession))).all()
    assert len(turns) == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_phonegate_restart_closes_open_session(
    profiled_factory: async_sessionmaker[AsyncSession], redis: object
) -> None:
    fake = FakePhoneGate()
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        loop = IngestLoop(client=client, session_factory=profiled_factory, redis=redis,
                          correlation=CallerCorrelation(), health=HealthTracker(),
                          settings=Settings(_env_file=None))
        await loop.load_cursor()
        fake.ring("+37360111222"); fake.answer()
        await _drain(loop, 3)
        fake.hangup()            # emit IDLE, then restart drops it below the cursor
        fake.restart()
        await _drain(loop, 3)
    async with profiled_factory() as session:
        call = (await session.scalars(select(CommunicationSession))).one()
    assert call.ended_at is not None
    assert call.needs_review is True
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/unit/test_phone_ingest_dispatch.py -q` → FAIL
(`AttributeError: run_cycle`).

- [ ] **Step 3: Implement (extend `app/phone/ingest.py`)**

Add imports:

```python
import asyncio
from collections.abc import Callable

from app.audit import record_audit_event
from app.phone.numbers import mask_phone
from app.phone.schemas import PhoneEvent
```

Add methods to `IngestLoop`:

```python
    async def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        status = await self._client.device_status()
        await self.reconcile(status)
        while not should_stop():
            active = await self.run_cycle()
            interval = (
                self._settings.phone_poll_active_seconds
                if active
                else self._settings.phone_poll_idle_seconds
            )
            await asyncio.sleep(interval)

    async def run_cycle(self) -> bool:
        """One poll iteration. Returns True when a call is currently active."""
        try:
            status = await self._client.device_status()
        except Exception as exc:  # PhoneGateUnavailable / PhoneGateError
            self._health.record_transport_error(type(exc).__name__)
            await self._persist_health()
            logger.warning("phone_status_poll_failed", error_type=type(exc).__name__)
            return False

        self._health.record_status(status)

        try:
            page = await self._client.events(after_id=self._cursor, limit=250)
        except Exception as exc:
            self._health.record_transport_error(type(exc).__name__)
            await self._persist_health()
            return status.call_state != "IDLE"

        if page.latest_id < self._cursor:
            logger.warning("phone_events_reset", latest_id=page.latest_id, cursor=self._cursor)
            await self.reconcile(status)
            await self.save_cursor(page.latest_id)
        elif page.events:
            ordered = sorted(page.events, key=lambda e: e.id)
            if ordered[0].id > self._cursor + 1:
                logger.warning(
                    "phone_events_gap", first=ordered[0].id, cursor=self._cursor
                )
                await self._flag_open_session_gap()
            async with self._session_factory() as session:
                for event in ordered:
                    await self._dispatch(session, event, status)
                await session.commit()
            await self.save_cursor(max(self._cursor, page.latest_id, ordered[-1].id))

        self._health.mark_poll_ok()
        await self._persist_health()
        return status.call_state != "IDLE"

    async def _flag_open_session_gap(self) -> None:
        async with self._session_factory() as session:
            open_row = await self._store.find_open(session)
            if open_row is not None:
                open_row.needs_review = True
                open_row.diagnostics = {**open_row.diagnostics, "note": "events_gap"}
                await session.commit()

    async def _persist_health(self) -> None:
        async with self._session_factory() as session:
            await self._health.persist(session)
            await session.commit()

    async def _dispatch(
        self, session: AsyncSession, event: PhoneEvent, status: DeviceStatus
    ) -> None:
        if event.type == "incoming_call":
            await self._on_incoming_call(session, event)
        elif event.type == "call_state":
            await self._on_call_state(session, event, status)
        elif event.type == "transcript":
            await self._on_transcript(session, event, status)

    async def _on_incoming_call(self, session: AsyncSession, event: PhoneEvent) -> None:
        raw = str(event.data.get("caller_number") or "")
        open_row = await self._store.find_open(session)
        if open_row is not None:
            if open_row.remote_raw == raw or (raw and open_row.remote_address == raw):
                return
            await self._store.close(
                session, open_row, outcome=CommunicationOutcome.ABANDONED,
                ended_at=utcnow(), note="superseded_by_new_caller",
            )
        correlation = await self._correlation.resolve(session, raw)
        if correlation is None:
            logger.error("phone_no_default_profile", caller=mask_phone(raw))
            return
        from app.phone.numbers import normalize_e164

        call = await self._store.open(
            session, remote_raw=raw,
            remote_address=normalize_e164(raw, region=self._settings.phone_caller_region) or "",
            event_id=event.id, correlation=correlation, opened_at=utcnow(),
        )
        self._open_session_id = call.id
        await record_audit_event(
            session, actor="phone-agent", action="communication_session.opened",
            entity_type="communication_session", entity_id=str(call.id),
            correlation_id=str(call.id),
            details={"caller": mask_phone(raw), "application_id": str(correlation.application_id),
                     "profile_id": str(correlation.profile_id)},
        )

    async def _on_call_state(
        self, session: AsyncSession, event: PhoneEvent, status: DeviceStatus
    ) -> None:
        state = str(event.data.get("state") or "")
        open_row = await self._store.find_open(session)
        if state == "RINGING" and open_row is not None:
            await self._store.touch_ringing(open_row, utcnow())
        elif state == "IN_CALL" and open_row is not None:
            await self._store.touch_answered(open_row, utcnow())
        elif state == "IDLE" and open_row is not None:
            outcome = (
                CommunicationOutcome.COMPLETED
                if open_row.answered_at is not None
                else CommunicationOutcome.MISSED
            )
            await self._store.close(
                session, open_row, outcome=outcome, ended_at=utcnow(),
                rx_stats=status.rx_audio_stats.model_dump(),
            )
            self._open_session_id = None

    async def _on_transcript(
        self, session: AsyncSession, event: PhoneEvent, status: DeviceStatus
    ) -> None:
        payload = event.data.get("transcript")
        if not isinstance(payload, dict):
            return
        from app.phone.schemas import TranscriptEntry

        entry = TranscriptEntry.model_validate(payload)
        open_row = await self._store.find_open(session)
        if open_row is None:
            if status.call_state == "IDLE":
                logger.info("phone_transcript_after_call_end", transcript_id=entry.id)
                return
            correlation = await self._correlation.resolve(session, status.caller_number)
            if correlation is None:
                return
            open_row = await self._store.open(
                session, remote_raw=status.caller_number,
                remote_address="", event_id=event.id, correlation=correlation,
                opened_at=utcnow(), needs_review=True,
                note="transcript_before_session_start",
            )
            self._open_session_id = open_row.id
        await self._store.append_turn(session, session_id=open_row.id, entry=entry)
```

Clean up: move the two inline `from app.phone.numbers import normalize_e164` /
`from app.phone.schemas import TranscriptEntry` imports to the module top.

Note on `run_cycle`: the small "advance cursor only after commit" block above has
a redundant branch — simplify to: on the reset path, `reconcile` + set cursor +
`save_cursor`; on the normal path, dispatch + `commit` + `save_cursor(new_cursor)`.
Keep exactly one `session.commit()` per cycle before `save_cursor`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_phone_ingest_dispatch.py tests/unit/test_phone_ingest_cursor.py -q`
→ PASS. `uv run mypy app` → clean. `uv run ruff check app/phone` → clean.

- [ ] **Step 5: Commit**

```bash
git add app/phone/ingest.py tests/unit/test_phone_ingest_dispatch.py
git commit -m "feat: ingest loop event dispatch and poll cycle"
```

---

## Task 15: Agent process and CLI subcommand

**Files:**
- Create: `app/phone/agent.py`
- Modify: `app/cli.py`
- Test: `tests/unit/test_phone_agent.py` (create)

**Interfaces:**
- Consumes: `get_settings`, `async_session_factory`,
  `leased_redis_lock` / `lock_key` / `close_redis_client` from
  `app.scheduler.locks`, `redis.Redis` (sync) + `redis.asyncio.Redis`,
  `IngestLoop`, `PhoneGateClient`, `HealthTracker`, `CallerCorrelation`.
- Produces `app.phone.agent`:
  - `HEARTBEAT_PATH = Path("/tmp/phone-agent-alive")` (overridable via
    `PHONE_AGENT_HEARTBEAT_PATH` env for tests)
  - `async run() -> int` — the process body; returns an exit code.
  - `main() -> None` — `raise SystemExit(asyncio.run(run()))`.
- `app/cli.py`: `job-agent phone-agent` subcommand calls `app.phone.agent.main()`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_phone_agent.py`:

```python
from __future__ import annotations

import pytest

from app.phone import agent as agent_module
from app.settings.config import Settings, get_settings


@pytest.mark.asyncio
async def test_run_exits_zero_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings, "cache_clear", lambda: None, raising=False)
    monkeypatch.setattr(agent_module, "get_settings", lambda: Settings(_env_file=None))
    code = await agent_module.run()
    assert code == 0


def test_cli_registers_phone_agent_subcommand() -> None:
    from app.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["phone-agent"])
    assert args.command == "phone-agent"
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_phone_agent.py -q` → FAIL.

- [ ] **Step 3: Implement**

Create `app/phone/agent.py`:

```python
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import structlog
from redis import Redis as SyncRedis
from redis.asyncio import Redis as AsyncRedis

from app.database import async_session_factory
from app.phone.client import PhoneGateClient
from app.phone.correlation import CallerCorrelation
from app.phone.health import HealthTracker
from app.phone.ingest import IngestLoop
from app.scheduler.locks import close_redis_client, leased_redis_lock, lock_key
from app.settings import get_settings

logger = structlog.get_logger(__name__)

HEARTBEAT_PATH = Path(os.getenv("PHONE_AGENT_HEARTBEAT_PATH", "/tmp/phone-agent-alive"))
_SINGLETON_LOCK_TTL = 60


async def _run_loop(*, lease_lost: "callable[[], bool]") -> None:
    settings = get_settings()
    assert settings.phonegate_auth_token is not None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async_redis: AsyncRedis = AsyncRedis.from_url(settings.redis_url, decode_responses=True)
    client = PhoneGateClient(
        base_url=settings.phonegate_url,
        token=settings.phonegate_auth_token.get_secret_value(),
        timeout=settings.phone_http_timeout_seconds,
    )
    ingest = IngestLoop(
        client=client, session_factory=async_session_factory, redis=async_redis,
        correlation=CallerCorrelation(region=settings.phone_caller_region),
        health=HealthTracker(), settings=settings,
    )
    await ingest.load_cursor()

    def _should_stop() -> bool:
        return stop.is_set() or lease_lost()

    try:
        # run_forever, but touch the heartbeat each iteration
        status = await client.device_status()
        await ingest.reconcile(status)
        while not _should_stop():
            active = await ingest.run_cycle()
            HEARTBEAT_PATH.touch()
            await asyncio.sleep(
                settings.phone_poll_active_seconds if active
                else settings.phone_poll_idle_seconds
            )
    finally:
        await client.aclose()
        await async_redis.aclose()


async def run() -> int:
    settings = get_settings()
    if not settings.phone_agent_enabled:
        logger.info("phone_agent_disabled")
        return 0
    if settings.phonegate_auth_token is None:
        logger.error("phone_agent_missing_token")
        return 2

    sync_redis: SyncRedis = SyncRedis.from_url(settings.redis_url, decode_responses=True)
    try:
        with leased_redis_lock(
            sync_redis, lock_key("phone-agent", "singleton"), ttl_seconds=_SINGLETON_LOCK_TTL
        ) as lease:
            if lease is None:
                logger.warning("phone_agent_not_singleton")
                return 1
            await _run_loop(lease_lost=lambda: lease.lease_lost)
            if lease.lease_lost:
                logger.warning("phone_agent_lease_lost")
                return 1
    finally:
        close_redis_client(sync_redis)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))
```

(Fix the type hint: `lease_lost: Callable[[], bool]` with
`from collections.abc import Callable`.)

In `app/cli.py`:
- In `build_parser`, add: `subparsers.add_parser("phone-agent", help="run the
  read-only PhoneGate call observer")`.
- In `main`, add branch:
  ```python
      elif args.command == "phone-agent":
          from app.phone.agent import main as phone_agent_main

          phone_agent_main()
  ```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_phone_agent.py tests/unit/test_cli.py -q` → PASS.
`uv run mypy app` → clean.

- [ ] **Step 5: Commit**

```bash
git add app/phone/agent.py app/cli.py tests/unit/test_phone_agent.py
git commit -m "feat: phone-agent process and CLI subcommand"
```

---

## Task 16: Phone API endpoints

**Files:**
- Create: `app/api/phone_routes.py`
- Modify: `app/main.py` (mount the router)
- Test: `tests/integration/test_phone_api.py` (create)

**Interfaces:**
- Consumes: `require_api_actor`, `get_session`, `public_model`; models
  `PhoneChannelHealth`, `CommunicationSession`, `CommunicationTurn`;
  `app.phone.health.channel_status` + `HealthComponent`;
  `app.phone.numbers.mask_phone`.
- Produces: `app.api.phone_routes.router` (`APIRouter(prefix="/api/v1/phone",
  tags=["phone"])`) with:
  - `GET /status`
  - `GET /sessions?limit=`
  - `GET /sessions/{session_id}`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_phone_api.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio

from app.database import get_session
from app.main import app
from app.models.entities import (
    CommunicationSession, CommunicationTurn, PhoneChannelHealth, UserProfile,
)
from app.models.enums import (
    CommunicationChannel, CommunicationDirection, CommunicationOutcome,
    PhoneComponentStatus, TurnSpeaker,
)


@pytest_asyncio.fixture
async def client(sqlite_session_factory, monkeypatch) -> httpx.AsyncClient:
    async def _override():
        async with sqlite_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    monkeypatch.setattr("app.api.dependencies.get_settings",
                        lambda: _settings_with_key())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 headers={"Authorization": "Bearer secret"}) as c:
        yield c
    app.dependency_overrides.clear()


def _settings_with_key():
    from app.security.auth import hash_api_key
    from app.settings.config import Settings

    return Settings(_env_file=None, mcp_api_keys_hashed=[hash_api_key("secret")])


@pytest.mark.asyncio
async def test_status_endpoint_reports_channel(client, sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        session.add(PhoneChannelHealth(
            component="phonegate_transport", status=PhoneComponentStatus.HEALTHY,
            updated_at=datetime.now(UTC),
        ))
        session.add(PhoneChannelHealth(
            component="a14_daemon", status=PhoneComponentStatus.DEGRADED,
            updated_at=datetime.now(UTC),
        ))
        await session.commit()

    body = (await client.get("/api/v1/phone/status")).json()
    assert body["channel"] == "degraded"
    assert {c["component"] for c in body["components"]} >= {"phonegate_transport", "a14_daemon"}


@pytest.mark.asyncio
async def test_sessions_list_and_detail(client, sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="d", is_default=True)
        session.add(profile)
        await session.flush()
        call = CommunicationSession(
            profile_id=profile.id, channel=CommunicationChannel.CALL, transport="phonegate",
            direction=CommunicationDirection.INBOUND, remote_address="+37360111222",
            remote_raw="+37360111222", phonegate_event_id_start=1,
            started_at=datetime.now(UTC), ended_at=datetime.now(UTC),
            outcome=CommunicationOutcome.COMPLETED,
        )
        session.add(call)
        await session.flush()
        session.add(CommunicationTurn(session_id=call.id, phonegate_transcript_id=1, seq=1,
                                      speaker=TurnSpeaker.EMPLOYER, text="hi",
                                      occurred_at=datetime.now(UTC)))
        await session.commit()
        call_id = call.id

    listing = (await client.get("/api/v1/phone/sessions")).json()
    assert listing["sessions"][0]["remote_address"] == "+373••••222"

    detail = (await client.get(f"/api/v1/phone/sessions/{call_id}")).json()
    assert detail["turns"][0]["text"] == "hi"


@pytest.mark.asyncio
async def test_status_requires_auth(sqlite_session_factory) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/api/v1/phone/status")).status_code == 401
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/integration/test_phone_api.py -q` → FAIL (404 / import).

- [ ] **Step 3: Implement**

Create `app/api/phone_routes.py`:

```python
from __future__ import annotations

# ruff: noqa: B008
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_actor
from app.database import get_session
from app.models.entities import CommunicationSession, CommunicationTurn, PhoneChannelHealth
from app.phone.health import HealthComponent, channel_status
from app.phone.numbers import mask_phone

router = APIRouter(prefix="/api/v1/phone", tags=["phone"])


@router.get("/status", dependencies=[Depends(require_api_actor)])
async def phone_status(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = list((await session.scalars(select(PhoneChannelHealth))).all())
    components = [
        HealthComponent(r.component, r.status, r.detail, r.last_ok_at) for r in rows
    ]
    agent_row = next((r for r in rows if r.component == "agent"), None)
    newest = await session.scalar(
        select(CommunicationSession).order_by(desc(CommunicationSession.started_at)).limit(1)
    )
    return {
        "channel": channel_status(components).value if components else "unknown",
        "agent": {
            "last_ok_at": agent_row.last_ok_at.isoformat()
            if agent_row and agent_row.last_ok_at
            else None,
            "status": agent_row.status.value if agent_row else "unknown",
        },
        "components": [
            {
                "component": r.component,
                "status": r.status.value,
                "detail": r.detail,
                "last_ok_at": r.last_ok_at.isoformat() if r.last_ok_at else None,
            }
            for r in rows
        ],
        "current_call": {
            "session_id": str(newest.id) if newest else None,
            "state": "connected"
            if newest and newest.ended_at is None and newest.answered_at is not None
            else ("ringing" if newest and newest.ended_at is None else "idle"),
            "caller_number": mask_phone(newest.remote_address) if newest else None,
        },
    }


def _session_row(call: CommunicationSession, turn_count: int) -> dict[str, Any]:
    return {
        "id": str(call.id),
        "profile_id": str(call.profile_id),
        "application_id": str(call.application_id) if call.application_id else None,
        "direction": call.direction.value,
        "remote_address": mask_phone(call.remote_address),
        "started_at": call.started_at.isoformat(),
        "answered_at": call.answered_at.isoformat() if call.answered_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "outcome": call.outcome.value if call.outcome else None,
        "needs_review": call.needs_review,
        "turn_count": turn_count,
    }


@router.get("/sessions", dependencies=[Depends(require_api_actor)])
async def list_sessions(
    limit: int = 50, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    calls = list(
        (
            await session.scalars(
                select(CommunicationSession)
                .order_by(desc(CommunicationSession.started_at))
                .limit(limit)
            )
        ).all()
    )
    counts = dict(
        (
            await session.execute(
                select(CommunicationTurn.session_id, func.count(CommunicationTurn.id))
                .where(CommunicationTurn.session_id.in_([c.id for c in calls] or [None]))
                .group_by(CommunicationTurn.session_id)
            )
        ).all()
    )
    return {"sessions": [_session_row(c, int(counts.get(c.id, 0))) for c in calls]}


@router.get("/sessions/{session_id}", dependencies=[Depends(require_api_actor)])
async def session_detail(
    session_id: UUID, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    call = await session.get(CommunicationSession, session_id)
    if call is None:
        raise HTTPException(status_code=404, detail="session not found")
    turns = list(
        (
            await session.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.session_id == session_id)
                .order_by(CommunicationTurn.seq)
            )
        ).all()
    )
    return {
        **_session_row(call, len(turns)),
        "diagnostics": call.diagnostics,
        "rx_frame_stats": call.rx_frame_stats,
        "turns": [
            {
                "seq": t.seq,
                "speaker": t.speaker.value,
                "text": t.text,
                "asr_backend": t.asr_backend,
                "asr_confidence": t.asr_confidence,
                "occurred_at": t.occurred_at.isoformat(),
            }
            for t in turns
        ],
    }
```

In `app/main.py`, after `app.include_router(admin_router)` add:

```python
from app.api.phone_routes import router as phone_router
app.include_router(phone_router)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/integration/test_phone_api.py -q` → PASS.
`uv run mypy app` → clean.

- [ ] **Step 5: Commit**

```bash
git add app/api/phone_routes.py app/main.py tests/integration/test_phone_api.py
git commit -m "feat: read-only phone status and sessions API"
```

---

## Task 17: Admin Диагностика — Phone channel section

**Files:**
- Modify: `app/admin/routes.py`
- Modify: `app/admin/templates/dashboard_diagnostics.html`
- Create: `app/admin/templates/_phone_health.html`
- Test: `tests/unit/test_admin_ui.py` (extend)

**Interfaces:**
- Consumes: model `PhoneChannelHealth`, `CommunicationSession`;
  `app.phone.health.channel_status` + `HealthComponent`.
- Produces: `phone_health` key in the dashboard template context (a dict with
  `channel`, `components`, `last_session`), rendered by `_phone_health.html`,
  included from the diagnostics template.

- [ ] **Step 1: Write the failing test (extend `tests/unit/test_admin_ui.py`)**

Add a test that logs into the admin panel (follow the existing helper in that
file), seeds a `PhoneChannelHealth` row, requests `/?view=diagnostics`, and
asserts the response body contains `Phone channel` / `phonegate_transport` and
the localized status label. Use the file's existing authenticated-client
fixture; mirror an existing diagnostics test.

```python
@pytest.mark.asyncio
async def test_diagnostics_shows_phone_channel(admin_client, db_session) -> None:
    from datetime import UTC, datetime
    from app.models.entities import PhoneChannelHealth
    from app.models.enums import PhoneComponentStatus

    db_session.add(PhoneChannelHealth(
        component="phonegate_transport", status=PhoneComponentStatus.HEALTHY,
        updated_at=datetime.now(UTC),
    ))
    await db_session.commit()

    body = (await admin_client.get("/?view=diagnostics")).text
    assert "Phone channel" in body
    assert "phonegate_transport" in body
```

(Adapt `admin_client` / `db_session` to the fixtures already present in
`tests/unit/test_admin_ui.py`.)

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/unit/test_admin_ui.py -k phone_channel -q` → FAIL.

- [ ] **Step 3: Implement**

In `app/admin/routes.py`, add a helper near `_pagination`:

```python
async def _phone_health(session: AsyncSession) -> dict[str, Any]:
    from app.models.entities import PhoneChannelHealth
    from app.phone.health import HealthComponent, channel_status

    rows = list((await session.scalars(select(PhoneChannelHealth))).all())
    components = [HealthComponent(r.component, r.status, r.detail, r.last_ok_at) for r in rows]
    return {
        "channel": channel_status(components).value if components else "unknown",
        "components": [
            {"component": r.component, "status": r.status.value, "detail": r.detail,
             "last_ok_at": r.last_ok_at}
            for r in sorted(rows, key=lambda r: r.component)
        ],
        "configured": get_settings().phone_agent_enabled,
    }
```

In the diagnostics branch (`else:` block around line 1204, where `active_alerts`
is loaded) add:

```python
        phone_health = await _phone_health(session)
```

Initialize `phone_health: dict[str, Any] = {}` alongside the other
view-scoped variables near the top of `dashboard`, and add `"phone_health":
phone_health,` to the template context dict.

Create `app/admin/templates/_phone_health.html`:

```html
<section class="card">
  <h3>Phone channel
    <span class="badge badge-{{ status_tone(phone_health.channel) }}">
      {{ status_label(phone_health.channel) }}</span>
  </h3>
  {% if not phone_health.configured %}
  <p class="muted">Телефонный агент выключен (PHONE_AGENT_ENABLED=false).</p>
  {% endif %}
  <ul class="component-list">
    {% for c in phone_health.components %}
    <li>
      <span class="status-dot status-dot-{{ status_tone(c.status) }}"></span>
      <code>{{ c.component }}</code>
      <span>{{ status_label(c.status) }}</span>
      {% if c.detail %}<span class="muted">{{ c.detail }}</span>{% endif %}
      {% if c.last_ok_at %}<time class="muted">{{ format_dt(c.last_ok_at) }}</time>{% endif %}
    </li>
    {% endfor %}
    {% if not phone_health.components %}
    <li class="muted">Нет данных — агент ещё не опрашивал PhoneGate.</li>
    {% endif %}
  </ul>
</section>
```

In `app/admin/templates/dashboard_diagnostics.html`, add near the other cards:
`{% include "_phone_health.html" %}`.

Add the localized labels to the maps in `app/admin/routes.py` if
`status_label` / `status_tone` do not already cover
`healthy`/`degraded`/`unavailable`/`unknown` (they cover `healthy`; add
`"degraded": "Деградация"`, `"unavailable": "Недоступно"`, `"unknown":
"Неизвестно"` to `_STATUS_LABELS` and appropriate tones to `_STATUS_TONES` —
check names in the file, lines ~110-160 and ~283-300).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_admin_ui.py -q` → PASS.
`uv run mypy app` → clean.

- [ ] **Step 5: Commit**

```bash
git add app/admin/ tests/unit/test_admin_ui.py
git commit -m "feat: phone channel section in admin diagnostics"
```

---

## Task 18: Compose service and operator docs

**Files:**
- Modify: `docker-compose.yml`
- Modify: `docker-compose.prod.yml` (if the app services are overridden there)
- Modify: `.env.example` (already touched in Task 4 — verify)
- Modify: `README.md`
- Test: `docker compose config --quiet`

**Interfaces:** none (deployment only).

- [ ] **Step 1: Add the Compose service**

In `docker-compose.yml`, after the `beat:` service, add:

```yaml
  call-agent:
    <<: *app
    command: ["job-agent", "phone-agent"]
    depends_on:
      migrate:
        condition: service_completed_successfully
      redis:
        condition: service_healthy
    healthcheck:
      <<: *app-healthcheck
      test:
        - CMD-SHELL
        - >-
          test -f /tmp/phone-agent-alive &&
          test -n "$$(find /tmp/phone-agent-alive -mmin -2)"
    restart: unless-stopped
    stop_grace_period: 15s
    logging: *default-logging
```

If `docker-compose.prod.yml` overrides `api`/`worker`/`beat` (image digest,
env), add a matching `call-agent:` override block there.

- [ ] **Step 2: Validate**

Run:
```bash
docker compose config --quiet
docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet
```
Expected: no output, exit 0.

- [ ] **Step 3: Document**

In `README.md`, add a short subsection under the architecture list:

```markdown
## Телефонный агент (Phase 1)

`call-agent` — отдельный процесс `job-agent phone-agent`, который только читает
PhoneGate REST API (`/api/events`, `/api/device/status`), сопоставляет входящие
звонки с откликами и сохраняет `CommunicationSession` / `CommunicationTurn`. Он
никогда не отвечает, не говорит и не звонит. По умолчанию выключен
(`PHONE_AGENT_ENABLED=false`); включается только после настройки `PHONEGATE_URL`
и `PHONEGATE_AUTH_TOKEN`. Здоровье канала — в разделе `Диагностика` и
`GET /api/v1/phone/status`. Деградация телефона не влияет на `/ready`.
```

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml docker-compose.prod.yml README.md .env.example
git commit -m "feat: call-agent compose service and operator docs"
```

---

## Task 19: Integration loop test and live smoke

**Files:**
- Create: `tests/integration/test_phone_ingest_loop.py`
- Create: `tests/integration/test_phone_live_smoke.py`
- Modify: `pyproject.toml` (no marker change needed — `live` marker exists)
- Test: the new files

**Interfaces:** none new — exercises the assembled loop end to end.

- [ ] **Step 1: Write the service-backed integration test**

Create `tests/integration/test_phone_ingest_loop.py`. It runs the full
`IngestLoop` against `FakePhoneGate` and a real database session factory (the
suite's Postgres-backed fixtures when `RUN_SERVICE_INTEGRATION_TESTS=1`, else
SQLite). Model the test on `tests/unit/test_phone_ingest_dispatch.py` but assert
the full persisted graph including `AuditEvent` rows and `phone_channel_health`
rows, and a restart-mid-call scenario followed by a fresh `IngestLoop`
(simulating an agent restart) that reconciles the dangling session.

```python
from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.entities import AuditEvent, CommunicationSession, PhoneChannelHealth, UserProfile
from app.models.enums import CommunicationOutcome
from app.phone.client import PhoneGateClient
from app.phone.correlation import CallerCorrelation
from app.phone.health import HealthTracker
from app.phone.ingest import IngestLoop
from app.settings.config import Settings
from tests.fixtures.fake_phonegate import FakePhoneGate


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _loop(client, factory, redis):
    return IngestLoop(client=client, session_factory=factory, redis=redis,
                      correlation=CallerCorrelation(), health=HealthTracker(),
                      settings=Settings(_env_file=None))


@pytest.mark.asyncio
async def test_agent_restart_reconciles_dangling_session(sqlite_session_factory, redis) -> None:
    async with sqlite_session_factory() as session:
        session.add(UserProfile(name="d", is_default=True))
        await session.commit()

    fake = FakePhoneGate()
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c1:
        loop1 = _loop(c1, sqlite_session_factory, redis)
        await loop1.load_cursor()
        fake.ring("+37360111222"); fake.answer()
        for _ in range(3):
            await loop1.run_cycle()

    # a brand-new loop == a restarted process
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c2:
        loop2 = _loop(c2, sqlite_session_factory, redis)
        await loop2.load_cursor()
        fake.hangup()
        for _ in range(3):
            await loop2.run_cycle()

    async with sqlite_session_factory() as session:
        call = (await session.scalars(select(CommunicationSession))).one()
        audits = (await session.scalars(select(AuditEvent))).all()
        health = (await session.scalars(select(PhoneChannelHealth))).all()
    assert call.ended_at is not None
    assert call.outcome in {CommunicationOutcome.COMPLETED, CommunicationOutcome.UNKNOWN}
    assert any(a.action == "communication_session.opened" for a in audits)
    assert {h.component for h in health} >= {"phonegate_transport", "a14_daemon", "agent"}
```

- [ ] **Step 2: Write the live smoke test**

Create `tests/integration/test_phone_live_smoke.py`:

```python
from __future__ import annotations

import os

import pytest

from app.phone.client import PhoneGateClient
from app.settings.config import Settings

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_live_phonegate_health_and_status() -> None:
    if os.getenv("ENABLE_LIVE_PHONEGATE_SMOKE_TEST") != "true":
        pytest.skip("opt-in live PhoneGate smoke test")
    settings = Settings()  # reads real .env / environment
    assert settings.phonegate_auth_token is not None
    async with PhoneGateClient(
        base_url=settings.phonegate_url,
        token=settings.phonegate_auth_token.get_secret_value(),
        timeout=settings.phone_http_timeout_seconds,
    ) as client:
        assert (await client.health()).get("status") == "ok"
        status = await client.device_status()
        assert status.call_state in {"IDLE", "RINGING", "IN_CALL"}
```

- [ ] **Step 3: Run the tests**

Run:
```bash
uv run pytest tests/integration/test_phone_ingest_loop.py -q
uv run pytest -m live -q            # expect: skipped (opt-in flag not set)
```
Expected: first PASS, second all-skipped.

- [ ] **Step 4: Full verification**

Run:
```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy app fixture_site
uv run pytest -q
uv run alembic upgrade head && uv run alembic check
docker compose config --quiet
```
All must pass; `alembic check` prints "No new upgrade operations detected."

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_phone_ingest_loop.py tests/integration/test_phone_live_smoke.py
git commit -m "test: phone ingest integration and opt-in live smoke"
```

---

## Self-review

**Spec coverage:**

| Spec section | Task |
| --- | --- |
| §4.1 client | 8 |
| §4.2 schemas | 7 |
| §4.3 states | 7 |
| §5.1 entrypoint / §5.2 compose / §5.4 singleton | 15, 18 |
| §5.3 settings | 4 |
| §6.1 enums / §6.2–§6.6 tables / §6.7 migration | 1, 2, 3 |
| §7.1 poll cycle / §7.2 dispatch / §7.3 close-on-IDLE | 14 |
| §7.4 cursor / §7.5 reconcile / §7.6 gap guard | 13, 14 |
| §7.7 audit | 14 |
| §8 correlation (+ lazy phone-contact discovery) | 9 |
| §9.1 health components / §9.2 status API / §9.3 sessions API | 12, 16 |
| §9.4 admin Диагностика | 17 |
| §10 edge cases | 14 (tests), 19 |
| §11 testing (fake, unit, integration, migration, e2e, live) | 6, 19, and per-task tests |
| §12 file-by-file | all |

§7.6 buffer-eviction gap logging: covered in Task 14's `run_cycle`
(`phone_events_gap` warning + `_flag_open_session_gap`).

**Placeholder scan:** No "TBD"/"handle errors appropriately". Test code is
concrete. The two spots that say "adapt to existing fixtures" (Task 9 `Application`
kwargs, Task 17 `admin_client`) point at real, readable code the implementer
opens — acceptable, not a placeholder.

**Type consistency:** `IngestLoop.run_cycle` returns `bool` (used by
`run_forever` / agent sleep selection) — consistent across Tasks 13, 14, 15.
`CorrelationResult` fields match between Task 9 (produced) and Tasks 10, 14
(consumed). `SessionStore` method names (`open`, `close`, `find_open`,
`touch_ringing`, `touch_answered`, `append_turn`) are stable across Tasks 10,
11, 13, 14. `HealthTracker` method names (`record_status`,
`record_transport_error`, `mark_poll_ok`, `components`, `persist`) stable across
Tasks 12, 14, 16, 17. `channel_status` signature stable across Tasks 12, 16, 17.

---

## Execution handoff

After the plan is approved, implement with **superpowers:subagent-driven-development**
(recommended) — one fresh subagent per task, review between tasks — or
**superpowers:executing-plans** for batched inline execution.
