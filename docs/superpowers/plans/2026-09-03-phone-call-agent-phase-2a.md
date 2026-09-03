# Phone Call Agent — Phase 2a Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** JobHunter autonomously answers every inbound call (when enabled), plays a fixed Russian opening, listens and records, plays a fixed closing, and hangs up — with an operator kill switch and per-call controls, validated by real GSM calls before merge.

**Architecture:** A new `CallOrchestrator` (`app/phone/orchestrator.py`) drives one answered call through a small state machine (`POST_CONNECT_WAIT → GREETING → LISTENING → CLOSING → DONE`). An `OrchestratorSupervisor` spawns it as an `asyncio` task when Phase 1's `IngestLoop` sees `RINGING` and the auto-answer policy says yes. Phase 1's `IngestLoop` keeps polling and persisting `rx` transcript turns in parallel; the orchestrator writes only assistant (`tx`) turns via new `SessionStore` helpers. JobHunter does not synthesize speech — `POST /api/call/speak` sends plain text and PhoneGate's Piper synthesizes and plays it.

**Tech Stack:** FastAPI · async SQLAlchemy 2 · Alembic (`migrations/`) · `uv` · `httpx.AsyncClient` · `redis.asyncio` · `structlog` · pytest + pytest-asyncio · `FakePhoneGate` ASGI double.

**Spec:** `docs/superpowers/specs/2026-09-03-phone-call-agent-phase-2-design.md` — this plan implements **Phase 2a only** (spec §1.2). Spec §1.3 / §14 (evidence clips, post-call summary, expanded panel) are Phase 2b and **not** in this plan.

**Setup (do once, before Task 1):** From `main` (tip `9effa92`), create the feature branch:
```bash
git checkout main && git checkout -b feature/phone-call-agent-phase-2a
```
DEV Postgres is the `jobhunter-dev` compose project on `127.0.0.1:55432` (DB `dev`, user `dev`, password from `.env` `POSTGRES_PASSWORD`). Confirm with `docker compose ls` before any DB command.

## Global Constraints

Copied from spec §11. Every task's requirements implicitly include this section.

- `from __future__ import annotations` at the top of every module you touch. `mypy` is strict on `app/`. pytest runs with `filterwarnings=["error"]` — any warning fails a test; test output must be pristine (use `async with PhoneGateClient(...)`).
- `PhoneGateClient` gains **only** `answer` / `speak` / `hangup` — never `dial` / `send_sms`.
- `app/observability/health.py::readiness_status()` must not change; phone degradation must not affect `/ready`.
- Caller numbers masked (`+373••••NNN` via `app.phone.numbers.mask_phone`) everywhere they surface — logs, `AuditEvent` details, API responses, admin templates. `spoken_text` is assistant text (safe to store/show verbatim).
- No real external calls / SMS in CI. `FakePhoneGate` and `httpx.MockTransport` only. Real-call tests are opt-in (`ENABLE_REALCALL_TESTS=true`), never run in CI.
- Tests must not read the operator's local `.env` (existing conftest autouse fixture enforces it — use `Settings(_env_file=None, ...)`).
- DEV only. Never touch PROD compose project `jobhunter` / `/srv/jobhunter-prod`; never restart/deploy `/srv/phonegate`.
- Migrations verified with `alembic upgrade head && alembic check` against DEV Postgres 16.
- `job-agent phone-agent` stays the process; `call-agent` stays the Compose service. `PHONE_AGENT_ENABLED=false` and `phone_auto_answer_enabled=false` are the shipping defaults.
- Commit prefix `feat:` / `fix:` / `test:`; English commit messages. Commit-message trailer:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01KZa4B2AZAPZsKTVsQbnLSR
  ```
- The repo keeps a linear history (fast-forward merges, no merge commits).
- Verification sweep per task: `uv run ruff check <touched paths>`, `uv run ruff format --check <touched paths>`, `uv run mypy app fixture_site`, `uv run pytest -q <relevant test files>` then `uv run pytest -q` (full). A migration task also runs `alembic upgrade head && alembic check` against DEV Postgres. Note: repo-wide `ruff format --check` flags 7 pre-existing non-feature files (`test_matching.py`, `test_policy_and_email.py`, `app/reports/service.py`, `app/scheduler/tasks.py`, `test_scan_pipeline.py`, and 2 design-doc markdown files) — a ruff 0.16.1 artifact, **not yours**; only check the files you touched.

---

## File map

**Created:**
- `app/phone/script.py` — fixed Russian phrase-block constants.
- `app/phone/policy.py` — `AnswerDecision`, `should_answer(...)`.
- `app/phone/speak.py` — the speak-state fence, `speak_block(...)`, TX-delivery observation, exceptions.
- `app/phone/orchestrator.py` — `CallOrchestrator` (one call) and `OrchestratorSupervisor` (spawn/monitor + policy + Redis stop + per-call commands).
- `migrations/versions/<12hex>_phone_phase_2a.py`
- `tests/unit/test_phone_script.py`, `tests/unit/test_phone_policy.py`, `tests/unit/test_phone_speak.py`, `tests/unit/test_phone_orchestrator.py`
- `tests/integration/test_phone_orchestrator_loop.py`
- `tests/realcall/__init__.py`, `tests/realcall/conftest.py`, `tests/realcall/a06_originate.py`, `tests/realcall/test_realcall_phase_2a.py`, `tests/unit/test_realcall_preconditions.py`

**Modified:**
- `app/models/enums.py` — add `TurnDeliveryStatus`.
- `app/models/entities.py` — `CommunicationTurn.delivery_status` + `.spoken_text`; `CommunicationSession.auto_answered` + `.script_stage`.
- `app/phone/schemas.py` — `DeviceStatus.tx_active` + `.tx_preparing`.
- `app/phone/client.py` — `_post`, `answer/speak/hangup`, `PhoneGateBusy`.
- `app/phone/sessions.py` — `record_assistant_turn`, `set_turn_delivery`, `set_script_stage`, `mark_auto_answered`; fix `speaker_from_phonegate`.
- `app/phone/ingest.py` — `self._last_status` + `last_status` property; `_on_transcript` skips `tx`.
- `app/phone/agent.py` — build + tick the supervisor; restart-mid-call marking.
- `app/settings/config.py` — new settings.
- `app/api/phone_routes.py` — `auto_answer` block in `/status`.
- `app/admin/routes.py` — `_phone_health` additions; 4 new POST routes.
- `app/admin/templates/_phone_health.html` — auto-answer block + per-call buttons.
- `tests/fixtures/fake_phonegate.py` — `answer/speak/hangup` + TX-state simulation.
- `tests/fixtures/fake_redis.py` — add `delete`.
- `tests/unit/test_fake_phonegate.py`, `tests/unit/test_phone_client.py`, `tests/unit/test_phone_schemas.py`, `tests/unit/test_phone_sessions.py`, `tests/unit/test_phone_ingest_dispatch.py`, `tests/unit/test_phone_migration.py`, `tests/integration/test_sqlite_migrations.py`, `tests/integration/test_phone_api.py`, `tests/unit/test_admin_ui.py` — extend.

---

### Task 1: DB shape — enum, model fields, migration

**Files:**
- Modify: `app/models/enums.py` (after `class TurnSpeaker`)
- Modify: `app/models/entities.py` — `CommunicationTurn` (~line 663), `CommunicationSession` (~line 646)
- Create: `migrations/versions/e5f6a7b8c9d0_phone_phase_2a.py`
- Modify: `tests/unit/test_phone_migration.py`, `tests/integration/test_sqlite_migrations.py`
- Test: `tests/unit/test_phone_migration.py`

**Interfaces:**
- Produces: enum `TurnDeliveryStatus(StrEnum)` with `NOT_APPLICABLE="not_applicable"`, `ATTEMPTED="attempted"`, `DELIVERED="delivered"`, `DELIVERY_UNKNOWN="delivery_unknown"`, `FAILED="failed"`. `CommunicationTurn.delivery_status: TurnDeliveryStatus`, `CommunicationTurn.spoken_text: str | None`. `CommunicationSession.auto_answered: bool`, `CommunicationSession.script_stage: str | None`. Alembic head becomes `e5f6a7b8c9d0`.

- [ ] **Step 1: Add the enum**

In `app/models/enums.py`, immediately after `class TurnSpeaker(StrEnum): ...`:

```python
class TurnDeliveryStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    ATTEMPTED = "attempted"
    DELIVERED = "delivered"
    DELIVERY_UNKNOWN = "delivery_unknown"
    FAILED = "failed"
```

- [ ] **Step 2: Add the model columns**

In `app/models/entities.py`, `CommunicationTurn`, after `asr_meta`:

```python
    delivery_status: Mapped[TurnDeliveryStatus] = mapped_column(
        enum_column(TurnDeliveryStatus),
        default=TurnDeliveryStatus.NOT_APPLICABLE,
        nullable=False,
    )
    spoken_text: Mapped[str | None] = mapped_column(Text)
```

In `CommunicationSession`, after `needs_review`:

```python
    auto_answered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    script_stage: Mapped[str | None] = mapped_column(String(32))
```

Add `TurnDeliveryStatus` to the `from app.models.enums import (...)` block in `entities.py`.

- [ ] **Step 3: Write the failing migration test**

In `tests/unit/test_phone_migration.py`, add:

```python
@pytest.mark.asyncio
async def test_phase_2a_columns_present_after_metadata_create(sqlite_engine: AsyncEngine) -> None:
    def _cols(sync_conn: object, table: str) -> set[str]:
        return {c["name"] for c in inspect(sync_conn).get_columns(table)}

    async with sqlite_engine.connect() as conn:
        turns = await conn.run_sync(_cols, "communication_turns")
        sessions = await conn.run_sync(_cols, "communication_sessions")

    assert {"delivery_status", "spoken_text"} <= turns
    assert {"auto_answered", "script_stage"} <= sessions
```

- [ ] **Step 4: Run it — passes already** (metadata-create reflects the model)

Run: `uv run pytest -q tests/unit/test_phone_migration.py::test_phase_2a_columns_present_after_metadata_create`
Expected: PASS (this test guards the model, not the migration).

- [ ] **Step 5: Write the migration**

`migrations/versions/e5f6a7b8c9d0_phone_phase_2a.py` — match the style of
`migrations/versions/d4e5f6a7b8c9_communication_session_generation.py`:

```python
"""phone phase 2a: assistant-turn delivery + auto-answer session fields

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("communication_turns") as batch_op:
        batch_op.add_column(
            sa.Column(
                "delivery_status",
                sa.String(length=20),
                nullable=False,
                server_default="not_applicable",
            )
        )
        batch_op.add_column(sa.Column("spoken_text", sa.Text(), nullable=True))
    with op.batch_alter_table("communication_turns") as batch_op:
        batch_op.alter_column("delivery_status", server_default=None)

    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_answered", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(sa.Column("script_stage", sa.String(length=32), nullable=True))
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.alter_column("auto_answered", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.drop_column("script_stage")
        batch_op.drop_column("auto_answered")
    with op.batch_alter_table("communication_turns") as batch_op:
        batch_op.drop_column("spoken_text")
        batch_op.drop_column("delivery_status")
```

(`enum_column` renders as a plain string column on SQLite and a native enum only where configured; the existing phone enums use `sa.String`-shaped migrations — mirror that with `sa.String(length=20)`.)

- [ ] **Step 6: Bump the migration-head assertions**

In `tests/integration/test_sqlite_migrations.py`, replace both
`assert revision == ("d4e5f6a7b8c9",)` with `assert revision == ("e5f6a7b8c9d0",)`, and after the
`phonegate_generation` column assertion add:

```python
        assert {"delivery_status", "spoken_text"} <= {
            column["name"] for column in database.get_columns("communication_turns")
        }
        assert {"auto_answered", "script_stage"} <= {
            column["name"] for column in database.get_columns("communication_sessions")
        }
```

- [ ] **Step 7: Verify migration on DEV Postgres**

```bash
cd /home/andrei/JobHunter
PW=$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2- | tr -d '"')
ENVIRONMENT=test DATABASE_URL="postgresql+asyncpg://dev:${PW}@127.0.0.1:55432/dev" uv run alembic upgrade head
ENVIRONMENT=test DATABASE_URL="postgresql+asyncpg://dev:${PW}@127.0.0.1:55432/dev" uv run alembic check
```
Expected: upgrade runs `e5f6a7b8c9d0`; check prints `No new upgrade operations detected.`

- [ ] **Step 8: Run tests + sweep**

```bash
uv run pytest -q tests/unit/test_phone_migration.py tests/integration/test_sqlite_migrations.py tests/unit/test_phone_entities.py
uv run ruff check app/models migrations/versions/e5f6a7b8c9d0_phone_phase_2a.py
uv run ruff format --check app/models/enums.py app/models/entities.py migrations/versions/e5f6a7b8c9d0_phone_phase_2a.py
uv run mypy app fixture_site
uv run pytest -q
```

- [ ] **Step 9: Commit**

```bash
git add app/models/enums.py app/models/entities.py migrations/versions/e5f6a7b8c9d0_phone_phase_2a.py tests/unit/test_phone_migration.py tests/integration/test_sqlite_migrations.py
git commit -m "feat: phase 2a db shape — assistant-turn delivery + auto-answer session fields"
```

---

### Task 2: Settings

**Files:**
- Modify: `app/settings/config.py` (phone block ~line 48–55)
- Modify: `.env.example` (phone section)
- Test: `tests/unit/test_phone_settings.py`

**Interfaces:**
- Produces: `settings.phone_auto_answer_enabled: bool`, `settings.phone_answer_blocklist: list[str]`, and the timing settings named in spec §13. Blocklist entries are normalized E.164 strings.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_phone_settings.py`, add:

```python
def test_phase_2a_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.phone_auto_answer_enabled is False
    assert s.phone_answer_blocklist == []
    assert s.phone_answer_connect_timeout_seconds == 8.0
    assert s.phone_post_connect_wait_seconds == 1.5
    assert s.phone_speak_fence_timeout_seconds == 5.0
    assert s.phone_inter_block_listen_seconds == 0.8
    assert s.phone_listen_silence_timeout_seconds == 20.0
    assert s.phone_call_hard_cap_seconds == 180.0
    assert s.phone_orchestrator_poll_seconds == 0.15


def test_blocklist_is_normalized() -> None:
    s = Settings(_env_file=None, phone_answer_blocklist=["+373 60 111 222", "060999888"])
    assert s.phone_answer_blocklist == ["+37360111222", "+37360999888"]
```

- [ ] **Step 2: Run — fails** (`AttributeError` / value mismatch)

Run: `uv run pytest -q tests/unit/test_phone_settings.py -k phase_2a`
Expected: FAIL.

- [ ] **Step 3: Add the settings**

In `app/settings/config.py`, in the phone block after `phone_health_stale_after_seconds`:

```python
    phone_auto_answer_enabled: bool = False
    phone_answer_blocklist: list[str] = Field(default_factory=list)
    phone_answer_connect_timeout_seconds: float = Field(default=8.0, ge=2, le=30)
    phone_post_connect_wait_seconds: float = Field(default=1.5, ge=0.5, le=5)
    phone_speak_fence_timeout_seconds: float = Field(default=5.0, ge=1, le=15)
    phone_inter_block_listen_seconds: float = Field(default=0.8, ge=0.2, le=5)
    phone_listen_silence_timeout_seconds: float = Field(default=20.0, ge=5, le=120)
    phone_call_hard_cap_seconds: float = Field(default=180.0, ge=30, le=1800)
    phone_orchestrator_poll_seconds: float = Field(default=0.15, ge=0.05, le=1)
```

Add a validator right after the phone block (mirror `normalize_google_admin_emails` style):

```python
    @field_validator("phone_answer_blocklist", mode="after")
    @classmethod
    def _normalize_blocklist(cls, value: list[str]) -> list[str]:
        from app.phone.numbers import normalize_e164

        out: list[str] = []
        for item in value:
            e164 = normalize_e164(item, region="MD")
            if e164:
                out.append(e164)
        return out
```

(`region="MD"` is fine — the blocklist is small and operator-curated; `phone_caller_region` is not yet parsed when this validator runs.)

- [ ] **Step 4: Document in `.env.example`**

Under the existing phone section, add:

```
# Phase 2a: autonomous call answering. Ships OFF. Flip to true only after the
# real-call canary (pytest -m realcall) is green against A14.
PHONE_AUTO_ANSWER_ENABLED=false
# Comma/JSON list of E.164 numbers to never answer, e.g. ["+37360000000"]
PHONE_ANSWER_BLOCKLIST=[]
```

- [ ] **Step 5: Run + sweep + commit**

```bash
uv run pytest -q tests/unit/test_phone_settings.py
uv run ruff check app/settings tests/unit/test_phone_settings.py
uv run ruff format --check app/settings/config.py tests/unit/test_phone_settings.py
uv run mypy app fixture_site
uv run pytest -q
git add app/settings/config.py .env.example tests/unit/test_phone_settings.py
git commit -m "feat: phase 2a settings — auto-answer flag, blocklist, call timings"
```

---

### Task 3: `FakePhoneGate` write endpoints + TX-state simulation

**Files:**
- Modify: `tests/fixtures/fake_phonegate.py`
- Modify: `tests/fixtures/fake_redis.py`
- Test: `tests/unit/test_fake_phonegate.py`

**Interfaces:**
- Produces on `FakePhoneGate`: routes `POST /api/call/answer`, `POST /api/call/speak`, `POST /api/call/hangup`; status fields `tx_active` / `tx_preparing` reflecting an internal TX state machine; helpers `set_tx_auto_advance(bool)`, `advance_tx()`, `fail_next_speak(mode: Literal["409_tx_busy","409_transitional","timeout"])`, `answered_by_agent` flag. `FakeAsyncRedis.delete(key)`.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_fake_phonegate.py`, add:

```python
@pytest.mark.asyncio
async def test_answer_speak_hangup_and_tx_cycle() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    transport = fake.transport()
    async with httpx.AsyncClient(transport=transport, base_url="http://pg") as c:
        # speak before answer -> 409
        r = await c.post("/api/call/speak", json={"text": "hi"}, headers={"authorization": "Bearer t"})
        assert r.status_code == 409

        r = await c.post("/api/call/answer", headers={"authorization": "Bearer t"})
        assert r.status_code == 200
        st = (await c.get("/api/device/status", headers={"authorization": "Bearer t"})).json()
        assert st["call_state"] == "IN_CALL"

        r = await c.post("/api/call/speak", json={"text": "Здравствуйте"}, headers={"authorization": "Bearer t"})
        assert r.status_code == 200
        # a tx transcript line was written
        tr = (await c.get("/api/call/transcript?after_id=0", headers={"authorization": "Bearer t"})).json()
        assert any(e["speaker"] == "tx" and e["text"] == "Здравствуйте" for e in tr["entries"])
        # TX runs preparing -> active -> idle across status polls
        seen = []
        for _ in range(4):
            s = (await c.get("/api/device/status", headers={"authorization": "Bearer t"})).json()
            seen.append((s["tx_preparing"], s["tx_active"]))
        assert (True, False) in seen and (False, True) in seen and seen[-1] == (False, False)

        r = await c.post("/api/call/hangup", headers={"authorization": "Bearer t"})
        assert r.status_code == 200
        st = (await c.get("/api/device/status", headers={"authorization": "Bearer t"})).json()
        assert st["call_state"] == "IDLE"


@pytest.mark.asyncio
async def test_fail_next_speak_409_and_timeout() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with httpx.AsyncClient(transport=fake.transport(), base_url="http://pg") as c:
        await c.post("/api/call/answer", headers={"authorization": "Bearer t"})
        fake.fail_next_speak(mode="409_tx_busy")
        r = await c.post("/api/call/speak", json={"text": "x"}, headers={"authorization": "Bearer t"})
        assert r.status_code == 409
        # next call succeeds
        r = await c.post("/api/call/speak", json={"text": "y"}, headers={"authorization": "Bearer t"})
        assert r.status_code == 200
```

(Add `import httpx` and `import pytest` to the test file if missing.)

For `FakeAsyncRedis.delete`, in `tests/unit/` wherever `FakeAsyncRedis` is unit-tested, or add a one-liner assert in an existing fake_redis test: `await r.set("k","1"); await r.delete("k"); assert await r.get("k") is None`.

- [ ] **Step 2: Run — fails** (routes 404 / helpers missing)

- [ ] **Step 3: Implement**

`tests/fixtures/fake_redis.py` — add:

```python
    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
```

`tests/fixtures/fake_phonegate.py`:

- In `__init__`, add:
  ```python
  self._tx_preparing = False
  self._tx_active = False
  self._tx_stage = 0  # 0 idle, 1 preparing, 2 active
  self._tx_auto_advance = True
  self._fail_next_speak: str | None = None
  self.answered_by_agent = False
  ```
- Add the three routes to the `Starlette(routes=[...])` list:
  ```python
  Route("/api/call/answer", self._answer_route, methods=["POST"]),
  Route("/api/call/speak", self._speak_route, methods=["POST"]),
  Route("/api/call/hangup", self._hangup_route, methods=["POST"]),
  ```
- Add scripting helpers:
  ```python
  def set_tx_auto_advance(self, value: bool) -> None:
      self._tx_auto_advance = value

  def advance_tx(self) -> None:
      # 0 -> 1 (preparing) -> 2 (active) -> 0 (idle)
      self._tx_stage = (self._tx_stage + 1) % 3
      self._tx_preparing = self._tx_stage == 1
      self._tx_active = self._tx_stage == 2

  def fail_next_speak(self, *, mode: str) -> None:
      self._fail_next_speak = mode
  ```
- In `_status`'s returned dict, replace the hard-coded `"tx_active": False, "tx_preparing": False` with `"tx_active": self._tx_active, "tx_preparing": self._tx_preparing`, and at the **end** of `_status`, before returning, auto-advance:
  ```python
  # (compute body first, then:)
  if self._tx_auto_advance and self._tx_stage != 0:
      self.advance_tx()
  return JSONResponse(body)
  ```
  Restructure `_status` to build `body = {...}` then optionally advance then `return JSONResponse(body)` — the body must reflect state *before* the advance.
- The route handlers:
  ```python
  async def _answer_route(self, request: Request) -> JSONResponse:
      if not self._auth_ok(request):
          return JSONResponse({"detail": "auth"}, status_code=401)
      if self._call_state != "RINGING":
          return JSONResponse({"detail": "not ringing"}, status_code=409)
      self._call_state = "IN_CALL"
      self.answered_by_agent = True
      self._emit("call_state", self._call_state_data())
      return JSONResponse({"success": True})

  async def _speak_route(self, request: Request) -> JSONResponse:
      if not self._auth_ok(request):
          return JSONResponse({"detail": "auth"}, status_code=401)
      if self._call_state != "IN_CALL":
          return JSONResponse({"detail": "no active call"}, status_code=409)
      mode, self._fail_next_speak = self._fail_next_speak, None
      if mode == "timeout":
          raise httpx.ReadTimeout("simulated speak timeout", request=None)  # type: ignore[arg-type]
      if mode in {"409_tx_busy", "409_transitional"}:
          return JSONResponse({"detail": mode}, status_code=409)
      body = await request.json()
      text = str(body.get("text", ""))
      tid = self._next_transcript_id
      self._next_transcript_id += 1
      record = {
          "id": tid, "speaker": "tx", "text": text, "meta": "", "backend": "piper",
          "confidence": None, "timestamp": "00:00:00", "timestamp_ms": int(time.time() * 1000),
      }
      self._transcripts.append(record)
      self._emit("transcript", {"transcript": record})
      self._tx_stage, self._tx_preparing, self._tx_active = 1, True, False
      return JSONResponse({"success": True, "text": text})

  async def _hangup_route(self, request: Request) -> JSONResponse:
      if not self._auth_ok(request):
          return JSONResponse({"detail": "auth"}, status_code=401)
      self._call_state, self._caller = "IDLE", ""
      self._tx_stage = self._tx_preparing = self._tx_active = 0, False, False  # reset
      self._emit("call_state", self._call_state_data())
      return JSONResponse({"success": True})
  ```
  Fix the `_hangup_route` reset line to `self._tx_stage, self._tx_preparing, self._tx_active = 0, False, False`.

  For the `"timeout"` mode: raising inside an ASGI handler surfaces to `httpx` as a transport error → `PhoneGateClient._post` should map it to `PhoneGateUnavailable` (matches a real network timeout, and Task 7 branches on that). Verify in Task 4 that the resulting exception type is what `_post` expects; if `ASGITransport` swallows it into a 500, instead make `"timeout"` mode `return JSONResponse({"detail": "x"}, status_code=504)` and have `_post` treat `>=500` as `PhoneGateUnavailable` (it already does). **Use the 504 form** — it is deterministic across httpx versions.

  So: `if mode == "timeout": return JSONResponse({"detail": "gateway timeout"}, status_code=504)`.

- [ ] **Step 4: Run — passes**

Run: `uv run pytest -q tests/unit/test_fake_phonegate.py`

- [ ] **Step 5: Sweep + commit**

```bash
uv run ruff check tests/fixtures tests/unit/test_fake_phonegate.py
uv run ruff format --check tests/fixtures/fake_phonegate.py tests/fixtures/fake_redis.py tests/unit/test_fake_phonegate.py
uv run mypy app fixture_site
uv run pytest -q
git add tests/fixtures/fake_phonegate.py tests/fixtures/fake_redis.py tests/unit/test_fake_phonegate.py
git commit -m "test: FakePhoneGate answer/speak/hangup + TX-state simulation"
```

---

### Task 4: `PhoneGateClient.answer / speak / hangup` + `DeviceStatus` TX fields

**Files:**
- Modify: `app/phone/schemas.py` (`DeviceStatus`)
- Modify: `app/phone/client.py`
- Test: `tests/unit/test_phone_schemas.py`, `tests/unit/test_phone_client.py`

**Interfaces:**
- Consumes: `FakePhoneGate` from Task 3.
- Produces: `DeviceStatus.tx_active: bool` (default `False`), `DeviceStatus.tx_preparing: bool` (default `False`). `PhoneGateClient.answer() -> None`, `PhoneGateClient.speak(text: str) -> None`, `PhoneGateClient.hangup() -> None`. `class PhoneGateBusy(PhoneGateError)` carrying `.detail: str` (the 409 body's `detail`, lowercased). `_post` raises `PhoneGateUnavailable` on `>=500`, `PhoneGateBusy` on `409`, `PhoneGateError` on other `4xx`, `PhoneGateUnavailable` on `httpx.RequestError`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_phone_schemas.py` — add:

```python
def test_device_status_tx_fields_default_false() -> None:
    st = DeviceStatus.model_validate({})
    assert st.tx_active is False and st.tx_preparing is False
    st = DeviceStatus.model_validate({"tx_active": True, "tx_preparing": True})
    assert st.tx_active is True and st.tx_preparing is True
```

`tests/unit/test_phone_client.py` — add:

```python
@pytest.mark.asyncio
async def test_write_methods_against_fake() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        assert (await c.device_status()).call_state == "IN_CALL"
        await c.speak("Здравствуйте")
        await c.hangup()
        assert (await c.device_status()).call_state == "IDLE"


@pytest.mark.asyncio
async def test_speak_409_raises_phonegate_busy() -> None:
    fake = FakePhoneGate()  # IDLE -> speak 409
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        with pytest.raises(PhoneGateBusy):
            await c.speak("x")


@pytest.mark.asyncio
async def test_speak_504_raises_unavailable() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        fake.fail_next_speak(mode="timeout")
        with pytest.raises(PhoneGateUnavailable):
            await c.speak("x")
```

Add `from app.phone.client import PhoneGateBusy` to the test imports.

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: Implement `DeviceStatus` fields**

`app/phone/schemas.py`, in `DeviceStatus` after `latest_event_id`:

```python
    tx_active: bool = False
    tx_preparing: bool = False
```

- [ ] **Step 4: Implement `_post` + write methods + `PhoneGateBusy`**

`app/phone/client.py`:

```python
class PhoneGateBusy(PhoneGateError):
    """PhoneGate returned 409 — call-state transition or a TX still in progress."""

    def __init__(self, path: str, detail: str) -> None:
        self.detail = detail.lower()
        super().__init__(f"{path}: 409 {detail}")
```

Add a `_post` method next to `_get`:

```python
    async def _post(self, path: str, json: dict[str, Any] | None = None) -> None:
        try:
            response = await self._client.post(path, json=json)
        except httpx.RequestError as exc:
            raise PhoneGateUnavailable(f"{path}: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise PhoneGateUnavailable(f"{path}: HTTP {response.status_code}")
        if response.status_code == 409:
            detail = ""
            try:
                detail = str(response.json().get("detail", ""))
            except ValueError:
                pass
            raise PhoneGateBusy(path, detail)
        if response.status_code >= 400:
            raise PhoneGateError(f"{path}: HTTP {response.status_code}")
```

Add the three methods after `transcript()`:

```python
    async def answer(self) -> None:
        await self._post("/api/call/answer")

    async def speak(self, text: str) -> None:
        await self._post("/api/call/speak", {"text": text})

    async def hangup(self) -> None:
        await self._post("/api/call/hangup")
```

- [ ] **Step 5: Run — passes**; **sweep**; **commit**

```bash
uv run pytest -q tests/unit/test_phone_client.py tests/unit/test_phone_schemas.py
uv run ruff check app/phone/client.py app/phone/schemas.py tests/unit/test_phone_client.py tests/unit/test_phone_schemas.py
uv run ruff format --check app/phone/client.py app/phone/schemas.py tests/unit/test_phone_client.py tests/unit/test_phone_schemas.py
uv run mypy app fixture_site
uv run pytest -q
git add app/phone/client.py app/phone/schemas.py tests/unit/test_phone_client.py tests/unit/test_phone_schemas.py
git commit -m "feat: PhoneGateClient answer/speak/hangup + DeviceStatus tx fields"
```

---

### Task 5: `app/phone/script.py` — phrase blocks

**Files:**
- Create: `app/phone/script.py`
- Test: `tests/unit/test_phone_script.py`

**Interfaces:**
- Produces: `SCRIPT_GREETING: tuple[str, ...]` (the ordered opening blocks), `SCRIPT_CLOSING: str`, `SCRIPT_CLOSING_INTERRUPTED: str`.

- [ ] **Step 1: Write the test**

`tests/unit/test_phone_script.py`:

```python
from __future__ import annotations

from app.phone.script import SCRIPT_CLOSING, SCRIPT_CLOSING_INTERRUPTED, SCRIPT_GREETING


def test_greeting_blocks_are_short_nonempty_strings() -> None:
    assert len(SCRIPT_GREETING) >= 3
    for block in SCRIPT_GREETING:
        assert isinstance(block, str)
        assert 0 < len(block) <= 200  # short blocks keep Piper + GSM quality up


def test_closing_blocks_present() -> None:
    assert SCRIPT_CLOSING and SCRIPT_CLOSING_INTERRUPTED
    assert SCRIPT_CLOSING != SCRIPT_CLOSING_INTERRUPTED
```

- [ ] **Step 2: Run — fails** (module missing)

- [ ] **Step 3: Create the module** (spec §4.2 baseline; wording is the operator's to finalize)

```python
from __future__ import annotations

# Fixed opening blocks, spoken in order via POST /api/call/speak. Short blocks so
# the caller gets a listening gap between them (half-duplex, spec §4.3) and so
# Piper synthesis and GSM downlink quality stay high (spec §4.2).
SCRIPT_GREETING: tuple[str, ...] = (
    "Здравствуйте. Если вы звоните по поводу вакансии или собеседования с Андреем — "
    "вы позвонили по адресу.",
    "Я — голосовой ассистент Андрея и помогаю согласовать собеседования от его имени.",
    "Важные дату, время и адрес я обязательно уточню и запишу.",
    "Подскажите, пожалуйста, по какой вакансии вы звоните?",
)

SCRIPT_CLOSING: str = "Спасибо, я записал. Андрей свяжется с вами. Всего доброго."

SCRIPT_CLOSING_INTERRUPTED: str = (
    "Извините, мне нужно прервать разговор. Андрей свяжется с вами. Всего доброго."
)
```

- [ ] **Step 4: Run — passes**; **sweep**; **commit**

```bash
uv run pytest -q tests/unit/test_phone_script.py
uv run ruff check app/phone/script.py tests/unit/test_phone_script.py
uv run ruff format --check app/phone/script.py tests/unit/test_phone_script.py
uv run mypy app fixture_site
git add app/phone/script.py tests/unit/test_phone_script.py
git commit -m "feat: phase 2a fixed call-script phrase blocks"
```

---

### Task 6: `app/phone/policy.py` — auto-answer decision

**Files:**
- Create: `app/phone/policy.py`
- Test: `tests/unit/test_phone_policy.py`

**Interfaces:**
- Consumes: `DeviceStatus` (`app.phone.schemas`), `Settings` (`app.settings.config`).
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class AnswerDecision:
      answer: bool
      reason: str  # "disabled_by_config"|"stopped_by_operator"|"blocklisted"|"not_ringing"|"answer"

  def should_answer(*, status: DeviceStatus, settings: Settings,
                    runtime_stopped: bool, normalized_caller: str | None) -> AnswerDecision
  ```

- [ ] **Step 1: Write the failing test**

`tests/unit/test_phone_policy.py`:

```python
from __future__ import annotations

import pytest

from app.phone.policy import should_answer
from app.phone.schemas import DeviceStatus
from app.settings.config import Settings


def _status(state: str = "RINGING") -> DeviceStatus:
    return DeviceStatus(call_state=state, caller_number="+37360111222")


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, phone_auto_answer_enabled=True, **kw)


@pytest.mark.parametrize(
    ("kwargs", "expected_answer", "expected_reason"),
    [
        (dict(settings=Settings(_env_file=None), runtime_stopped=False, normalized_caller="+37360111222"),
         False, "disabled_by_config"),
        (dict(settings=_settings(), runtime_stopped=True, normalized_caller="+37360111222"),
         False, "stopped_by_operator"),
        (dict(settings=_settings(phone_answer_blocklist=["+37360111222"]), runtime_stopped=False,
              normalized_caller="+37360111222"),
         False, "blocklisted"),
        (dict(settings=_settings(), runtime_stopped=False, normalized_caller="+37360111222"),
         True, "answer"),
    ],
)
def test_should_answer_table(kwargs, expected_answer, expected_reason) -> None:
    d = should_answer(status=_status(), **kwargs)
    assert d.answer is expected_answer
    assert d.reason == expected_reason


def test_not_ringing_is_ignored() -> None:
    d = should_answer(status=_status("IN_CALL"), settings=_settings(),
                      runtime_stopped=False, normalized_caller="+37360111222")
    assert d.answer is False and d.reason == "not_ringing"


def test_unknown_caller_still_answered() -> None:
    d = should_answer(status=_status(), settings=_settings(),
                      runtime_stopped=False, normalized_caller=None)
    assert d.answer is True and d.reason == "answer"
```

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: Implement**

```python
from __future__ import annotations

from dataclasses import dataclass

from app.phone.schemas import DeviceStatus
from app.settings.config import Settings


@dataclass(frozen=True, slots=True)
class AnswerDecision:
    answer: bool
    reason: str


def should_answer(
    *,
    status: DeviceStatus,
    settings: Settings,
    runtime_stopped: bool,
    normalized_caller: str | None,
) -> AnswerDecision:
    if not settings.phone_auto_answer_enabled:
        return AnswerDecision(False, "disabled_by_config")
    if runtime_stopped:
        return AnswerDecision(False, "stopped_by_operator")
    if normalized_caller is not None and normalized_caller in settings.phone_answer_blocklist:
        return AnswerDecision(False, "blocklisted")
    if status.call_state != "RINGING":
        return AnswerDecision(False, "not_ringing")
    return AnswerDecision(True, "answer")
```

- [ ] **Step 4: Run — passes**; **sweep**; **commit**

```bash
uv run pytest -q tests/unit/test_phone_policy.py
uv run ruff check app/phone/policy.py tests/unit/test_phone_policy.py
uv run ruff format --check app/phone/policy.py tests/unit/test_phone_policy.py
uv run mypy app fixture_site
git add app/phone/policy.py tests/unit/test_phone_policy.py
git commit -m "feat: phase 2a auto-answer policy"
```

---

### Task 7: `app/phone/speak.py` — fence + idempotent speak + TX-delivery observation

**Files:**
- Create: `app/phone/speak.py`
- Test: `tests/unit/test_phone_speak.py`

**Interfaces:**
- Consumes: `PhoneGateClient` (`answer/speak/hangup/device_status`), `PhoneGateBusy`, `PhoneGateUnavailable`, `PhoneGateError`, `DeviceStatus`.
- Produces:
  ```python
  class CallEnded(Exception): ...
  class SpeakFenceTimeout(Exception): ...

  async def wait_until_speakable(client, *, timeout: float, poll: float) -> None
      # returns when IN_CALL and not tx_active and not tx_preparing;
      # raises CallEnded if call left IN_CALL; SpeakFenceTimeout on timeout.

  @dataclass(frozen=True, slots=True)
  class SpeakResult:
      outcome: str  # "ok" | "ended" | "unknown"

  async def speak_block(client, text: str, *, fence_timeout: float, poll: float) -> SpeakResult
      # fence, then POST /api/call/speak with 409 handling (one bounded retry),
      # ambiguous-timeout -> SpeakResult("unknown"), CallEnded -> SpeakResult("ended").

  async def observe_tx_delivery(client, *, timeout: float, poll: float, start_grace: float) -> str
      # -> "delivered" | "failed" | "delivery_unknown"
  ```

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_phone_speak.py`:

```python
from __future__ import annotations

import pytest

from app.phone.client import PhoneGateClient
from app.phone.speak import (
    CallEnded,
    SpeakFenceTimeout,
    observe_tx_delivery,
    speak_block,
    wait_until_speakable,
)
from tests.fixtures.fake_phonegate import FakePhoneGate

FAST = dict(timeout=2.0, poll=0.01)


@pytest.mark.asyncio
async def test_fence_ok_when_in_call_and_tx_idle() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        await wait_until_speakable(c, **FAST)  # returns, no raise


@pytest.mark.asyncio
async def test_fence_raises_when_call_ended() -> None:
    fake = FakePhoneGate()  # IDLE
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        with pytest.raises(CallEnded):
            await wait_until_speakable(c, **FAST)


@pytest.mark.asyncio
async def test_fence_times_out_while_tx_busy() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    fake.set_tx_auto_advance(False)  # TX stays busy forever
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        await c.speak("x")  # sets tx_preparing, never advances
        with pytest.raises(SpeakFenceTimeout):
            await wait_until_speakable(c, timeout=0.2, poll=0.01)


@pytest.mark.asyncio
async def test_speak_block_ok() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        res = await speak_block(c, "Здравствуйте", fence_timeout=2.0, poll=0.01)
        assert res.outcome == "ok"


@pytest.mark.asyncio
async def test_speak_block_ambiguous_timeout_is_unknown() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        fake.fail_next_speak(mode="timeout")
        res = await speak_block(c, "x", fence_timeout=2.0, poll=0.01)
        assert res.outcome == "unknown"


@pytest.mark.asyncio
async def test_speak_block_409_then_retry_succeeds() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        fake.fail_next_speak(mode="409_tx_busy")
        res = await speak_block(c, "x", fence_timeout=2.0, poll=0.01)
        assert res.outcome == "ok"


@pytest.mark.asyncio
async def test_observe_tx_delivery_delivered() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        await c.speak("x")  # tx_preparing True; auto-advances on status polls
        result = await observe_tx_delivery(c, timeout=2.0, poll=0.01, start_grace=1.0)
        assert result == "delivered"


@pytest.mark.asyncio
async def test_observe_tx_delivery_failed_when_call_drops() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        await c.speak("x")
        await c.hangup()
        result = await observe_tx_delivery(c, timeout=2.0, poll=0.01, start_grace=0.05)
        assert result == "failed"
```

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: Implement `app/phone/speak.py`**

```python
from __future__ import annotations

import time
from dataclasses import dataclass

import anyio
import structlog

from app.phone.client import PhoneGateBusy, PhoneGateClient, PhoneGateError, PhoneGateUnavailable

logger = structlog.get_logger(__name__)

_ACTIVE_STATES = {"IN_CALL"}


class CallEnded(Exception):
    """The call left IN_CALL while we were waiting to speak."""


class SpeakFenceTimeout(Exception):
    """TX did not become idle within the fence timeout."""


async def wait_until_speakable(client: PhoneGateClient, *, timeout: float, poll: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        status = await client.device_status()
        if status.call_state not in _ACTIVE_STATES:
            raise CallEnded(status.call_state)
        if not status.tx_active and not status.tx_preparing:
            return
        if time.monotonic() >= deadline:
            raise SpeakFenceTimeout
        await anyio.sleep(poll)


@dataclass(frozen=True, slots=True)
class SpeakResult:
    outcome: str  # "ok" | "ended" | "unknown"


async def speak_block(
    client: PhoneGateClient, text: str, *, fence_timeout: float, poll: float
) -> SpeakResult:
    try:
        await wait_until_speakable(client, timeout=fence_timeout, poll=poll)
    except CallEnded:
        return SpeakResult("ended")
    except SpeakFenceTimeout:
        logger.warning("phone_speak_fence_timeout")
        return SpeakResult("unknown")

    try:
        await client.speak(text)
        return SpeakResult("ok")
    except PhoneGateBusy as exc:
        # Re-read and take exactly one bounded retry.
        try:
            await wait_until_speakable(client, timeout=fence_timeout, poll=poll)
        except CallEnded:
            return SpeakResult("ended")
        except SpeakFenceTimeout:
            return SpeakResult("unknown")
        try:
            await client.speak(text)
            return SpeakResult("ok")
        except PhoneGateBusy:
            logger.warning("phone_speak_still_busy_after_retry", detail=exc.detail)
            return SpeakResult("unknown")
        except PhoneGateUnavailable:
            return SpeakResult("unknown")
    except PhoneGateUnavailable:
        # Ambiguous: PhoneGate may already have accepted the utterance. Never retry.
        logger.warning("phone_speak_ambiguous_timeout")
        return SpeakResult("unknown")
    except PhoneGateError:
        return SpeakResult("unknown")


async def observe_tx_delivery(
    client: PhoneGateClient, *, timeout: float, poll: float, start_grace: float
) -> str:
    """After a 200 from /speak, watch the TX state to decide the turn's fate:
    'delivered' (TX activated then returned to idle), 'failed' (call ended, or TX
    never activated within start_grace), 'delivery_unknown' (TX stuck past timeout).
    """
    start = time.monotonic()
    deadline = start + timeout
    saw_active = False
    while True:
        try:
            status = await client.device_status()
        except (PhoneGateUnavailable, PhoneGateError):
            return "delivery_unknown"
        if status.call_state not in _ACTIVE_STATES:
            return "delivered" if saw_active else "failed"
        if status.tx_active or status.tx_preparing:
            saw_active = True
        elif saw_active:
            return "delivered"
        elif time.monotonic() - start >= start_grace:
            return "failed"
        if time.monotonic() >= deadline:
            return "delivery_unknown"
        await anyio.sleep(poll)
```

Check that `anyio` is already a dependency (it is — FastAPI/httpx pull it). If `uv run python -c "import anyio"` fails, use `import asyncio` and `asyncio.sleep`.

- [ ] **Step 4: Run — passes**; **sweep**; **commit**

```bash
uv run pytest -q tests/unit/test_phone_speak.py
uv run ruff check app/phone/speak.py tests/unit/test_phone_speak.py
uv run ruff format --check app/phone/speak.py tests/unit/test_phone_speak.py
uv run mypy app fixture_site
uv run pytest -q
git add app/phone/speak.py tests/unit/test_phone_speak.py
git commit -m "feat: phase 2a speak-state fence, idempotent speak, TX-delivery observation"
```

---

### Task 8: `SessionStore` — assistant turns, script stage, auto-answered

**Files:**
- Modify: `app/phone/sessions.py`
- Test: `tests/unit/test_phone_sessions.py`

**Interfaces:**
- Consumes: `TurnDeliveryStatus`, `TurnSpeaker` (`app.models.enums`), `CommunicationSession`, `CommunicationTurn`.
- Produces on `SessionStore`:
  ```python
  async def record_assistant_turn(self, session, *, session_id: UUID, phonegate_transcript_id: int | None,
                                  spoken_text: str, delivery_status: TurnDeliveryStatus,
                                  occurred_at: datetime) -> CommunicationTurn
  async def set_turn_delivery(self, session, *, turn_id: UUID, status: TurnDeliveryStatus) -> None
  async def set_script_stage(self, call: CommunicationSession, stage: str) -> None
  async def mark_auto_answered(self, call: CommunicationSession, when: datetime) -> None
  ```
  Also: `speaker_from_phonegate("tx")` now returns `TurnSpeaker.ASSISTANT`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_phone_sessions.py` — add (the file already has a `db` fixture + `_corr` helper):

```python
async def test_record_assistant_turn_and_delivery(db: AsyncSession) -> None:
    from app.models.enums import TurnDeliveryStatus, TurnSpeaker

    store = SessionStore()
    now = datetime.now(UTC)
    call = await store.open(
        db, remote_raw="+3736011", remote_address="+3736011", event_id=1,
        correlation=_corr(db.info["profile_id"]), opened_at=now,
    )
    await db.flush()
    turn = await store.record_assistant_turn(
        db, session_id=call.id, phonegate_transcript_id=7, spoken_text="Здравствуйте",
        delivery_status=TurnDeliveryStatus.ATTEMPTED, occurred_at=now,
    )
    assert turn.speaker is TurnSpeaker.ASSISTANT
    assert turn.spoken_text == "Здравствуйте"
    assert turn.seq == 1
    await store.set_turn_delivery(db, turn_id=turn.id, status=TurnDeliveryStatus.DELIVERED)
    await db.commit()
    refreshed = await db.get(type(turn), turn.id)
    assert refreshed is not None and refreshed.delivery_status is TurnDeliveryStatus.DELIVERED


async def test_set_script_stage_and_mark_auto_answered(db: AsyncSession) -> None:
    store = SessionStore()
    now = datetime.now(UTC)
    call = await store.open(
        db, remote_raw="+3736011", remote_address="+3736011", event_id=1,
        correlation=_corr(db.info["profile_id"]), opened_at=now,
    )
    await store.mark_auto_answered(call, now)
    await store.set_script_stage(call, "greeting")
    await db.commit()
    refreshed = await db.get(type(call), call.id)
    assert refreshed is not None
    assert refreshed.auto_answered is True
    assert refreshed.answered_at is not None
    assert refreshed.script_stage == "greeting"


def test_speaker_from_phonegate_tx_is_assistant() -> None:
    from app.models.enums import TurnSpeaker

    from app.phone.sessions import speaker_from_phonegate

    assert speaker_from_phonegate("tx") is TurnSpeaker.ASSISTANT
```

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: Implement**

`app/phone/sessions.py`:

- Change `speaker_from_phonegate`:
  ```python
  def speaker_from_phonegate(value: str) -> TurnSpeaker:
      return {"rx": TurnSpeaker.EMPLOYER, "tx": TurnSpeaker.ASSISTANT}.get(value, TurnSpeaker.SYSTEM)
  ```
- Add `TurnDeliveryStatus` to the enum import.
- Add methods to `SessionStore`:
  ```python
  async def record_assistant_turn(
      self,
      session: AsyncSession,
      *,
      session_id: UUID,
      phonegate_transcript_id: int | None,
      spoken_text: str,
      delivery_status: TurnDeliveryStatus,
      occurred_at: datetime,
  ) -> CommunicationTurn:
      count = await session.scalar(
          select(func.count(CommunicationTurn.id)).where(
              CommunicationTurn.session_id == session_id
          )
      )
      turn = CommunicationTurn(
          session_id=session_id,
          phonegate_transcript_id=phonegate_transcript_id,
          seq=int(count or 0) + 1,
          speaker=TurnSpeaker.ASSISTANT,
          text=spoken_text,
          raw_text=spoken_text,
          spoken_text=spoken_text,
          delivery_status=delivery_status,
          occurred_at=occurred_at,
      )
      session.add(turn)
      await session.flush()
      return turn

  async def set_turn_delivery(
      self, session: AsyncSession, *, turn_id: UUID, status: TurnDeliveryStatus
  ) -> None:
      turn = await session.get(CommunicationTurn, turn_id)
      if turn is not None:
          turn.delivery_status = status
          await session.flush()

  async def set_script_stage(self, call: CommunicationSession, stage: str) -> None:
      call.script_stage = stage

  async def mark_auto_answered(self, call: CommunicationSession, when: datetime) -> None:
      call.auto_answered = True
      if call.answered_at is None:
          call.answered_at = when
  ```

- [ ] **Step 4: Run — passes**; **sweep**; **commit**

```bash
uv run pytest -q tests/unit/test_phone_sessions.py
uv run ruff check app/phone/sessions.py tests/unit/test_phone_sessions.py
uv run ruff format --check app/phone/sessions.py tests/unit/test_phone_sessions.py
uv run mypy app fixture_site
uv run pytest -q
git add app/phone/sessions.py tests/unit/test_phone_sessions.py
git commit -m "feat: SessionStore assistant-turn, script-stage, auto-answered helpers"
```

---

### Task 9: `IngestLoop` — expose last status; skip `tx` transcript lines

**Files:**
- Modify: `app/phone/ingest.py`
- Test: `tests/unit/test_phone_ingest_dispatch.py`

**Interfaces:**
- Produces: `IngestLoop.last_status: DeviceStatus | None` (property; the status from the most recent successful `device_status()` in `run_cycle`, or `None` before the first). `_on_transcript` returns early for `entry.speaker == "tx"` — assistant turns are written only by `CallOrchestrator` (Task 10).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_phone_ingest_dispatch.py` — add:

```python
async def test_last_status_is_exposed_after_run_cycle(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    fake = FakePhoneGate()
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        loop = _make_loop(client, profiled_factory, redis)
        assert loop.last_status is None
        await loop.load_cursor()
        await loop.run_cycle()
        assert loop.last_status is not None
        assert loop.last_status.call_state == "IDLE"


async def test_tx_transcript_lines_are_not_persisted_by_ingest(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    fake = FakePhoneGate()
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        loop = _make_loop(client, profiled_factory, redis)
        await loop.load_cursor()
        status = await client.device_status()
        await loop.save_cursor(status.latest_event_id)
        fake.ring("+37360111222")
        fake.answer()
        fake.transcript(speaker="rx", text="по вакансии грузчика")
        fake.transcript(speaker="tx", text="Здравствуйте")  # assistant line -> ingest must skip
        fake.hangup()
        await _drain(loop)

    async with profiled_factory() as session:
        turns = (await session.scalars(select(CommunicationTurn))).all()
    assert [t.text for t in turns] == ["по вакансии грузчика"]
```

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: Implement**

In `IngestLoop.__init__`: `self._last_status: DeviceStatus | None = None`.

Add the property (near `open_session_id`):
```python
    @property
    def last_status(self) -> DeviceStatus | None:
        return self._last_status
```

In `run_cycle`, right after `self._health.record_status(status)` (first occurrence, ~line 295): `self._last_status = status`. Also after the in-branch re-fetch `status = await self._client.device_status()` in the reset branch (~line 341): `self._last_status = status`.

In `_on_transcript`, right after `entry = TranscriptEntry.model_validate(payload)` succeeds:
```python
        if entry.speaker == "tx":
            # Assistant speech — CallOrchestrator (phase 2a) owns these turns and
            # is the only writer that knows the spoken_text / delivery status.
            return
```

- [ ] **Step 4: Run — passes**; **sweep**; **commit**

```bash
uv run pytest -q tests/unit/test_phone_ingest_dispatch.py tests/unit/test_phone_ingest_cursor.py tests/integration/test_phone_ingest_loop.py
uv run ruff check app/phone/ingest.py tests/unit/test_phone_ingest_dispatch.py
uv run ruff format --check app/phone/ingest.py tests/unit/test_phone_ingest_dispatch.py
uv run mypy app fixture_site
uv run pytest -q
git add app/phone/ingest.py tests/unit/test_phone_ingest_dispatch.py
git commit -m "feat: IngestLoop exposes last_status; skips assistant tx transcript lines"
```

---

### Task 10: `CallOrchestrator`

**Files:**
- Create: `app/phone/orchestrator.py` (the `CallOrchestrator` class; `OrchestratorSupervisor` is Task 11)
- Test: `tests/unit/test_phone_orchestrator.py`

**Interfaces:**
- Consumes: `PhoneGateClient`, `async_sessionmaker[AsyncSession]`, `Settings`; `speak_block` / `observe_tx_delivery` / `wait_until_speakable` / `CallEnded` (`app.phone.speak`); `SessionStore` (`record_assistant_turn`, `set_turn_delivery`, `set_script_stage`, `mark_auto_answered`, `close`); `SCRIPT_GREETING` / `SCRIPT_CLOSING` / `SCRIPT_CLOSING_INTERRUPTED` (`app.phone.script`); `TurnDeliveryStatus`; `CommunicationOutcome`.
- Produces:
  ```python
  TERMINAL_STAGES = {"greeting_completed", "aborted_operator", "aborted_error", "aborted_restart"}

  class CallOrchestrator:
      def __init__(self, *, client, session_factory, settings,
                   command_check: Callable[[], Awaitable[str | None]] | None = None) -> None
      async def run(self, session_id: UUID) -> str  # returns the terminal script_stage
  ```
  `command_check` is an async callable returning `"hangup"`, `"mute"`, `"stop"`, or `None` — polled once per LISTENING iteration and once before each greeting block. Task 11 wires it to Redis; unit tests pass a stub.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_phone_orchestrator.py`:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import CommunicationOutcome, TurnDeliveryStatus, TurnSpeaker
from app.phone.client import PhoneGateClient
from app.phone.correlation import CorrelationResult
from app.phone.orchestrator import CallOrchestrator
from app.phone.sessions import SessionStore
from app.settings.config import Settings
from tests.fixtures.fake_phonegate import FakePhoneGate


def _fast_settings() -> Settings:
    return Settings(
        _env_file=None,
        phone_auto_answer_enabled=True,
        phone_post_connect_wait_seconds=0.01,
        phone_speak_fence_timeout_seconds=2.0,
        phone_inter_block_listen_seconds=0.01,
        phone_listen_silence_timeout_seconds=0.2,
        phone_call_hard_cap_seconds=5.0,
        phone_orchestrator_poll_seconds=0.01,
    )


@pytest_asyncio.fixture
async def factory(sqlite_session_factory: async_sessionmaker[AsyncSession]) -> async_sessionmaker[AsyncSession]:
    async with sqlite_session_factory() as s:
        s.add(UserProfile(name="d", is_default=True))
        await s.commit()
    return sqlite_session_factory


async def _open_ringing_session(factory: async_sessionmaker[AsyncSession]) -> "UUID":
    async with factory() as s:
        profile = (await s.scalars(select(UserProfile))).one()
        store = SessionStore()
        call = await store.open(
            s, remote_raw="+37360111222", remote_address="+37360111222", event_id=2,
            correlation=CorrelationResult(profile.id, None, None, None, None),
            opened_at=datetime.now(UTC),
        )
        await s.commit()
        return call.id


@pytest.mark.asyncio
async def test_happy_path_greeting_listen_closing(factory) -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        orch = CallOrchestrator(client=client, session_factory=factory, settings=_fast_settings())
        stage = await orch.run(session_id)

    assert stage == "greeting_completed"
    async with factory() as s:
        call = await s.get(CommunicationSession, session_id)
        turns = (await s.scalars(
            select(CommunicationTurn).where(CommunicationTurn.session_id == session_id)
        )).all()
    assert call.auto_answered is True
    assert call.script_stage == "greeting_completed"
    assistant = [t for t in turns if t.speaker is TurnSpeaker.ASSISTANT]
    assert len(assistant) == 5  # 4 greeting blocks + 1 closing
    assert all(t.delivery_status is TurnDeliveryStatus.DELIVERED for t in assistant)
    assert fake._call_state == "IDLE"  # hung up


@pytest.mark.asyncio
async def test_hard_cap_cuts_listening(factory) -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    settings = _fast_settings()
    object.__setattr__  # (settings is pydantic; build a fresh one instead)
    settings = Settings(**{**settings.model_dump(), "phone_listen_silence_timeout_seconds": 10.0,
                           "phone_call_hard_cap_seconds": 0.3})
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        orch = CallOrchestrator(client=client, session_factory=factory, settings=settings)
        stage = await orch.run(session_id)
    assert stage == "greeting_completed"  # cap -> closing -> done is still a clean finish


@pytest.mark.asyncio
async def test_call_drops_mid_greeting(factory) -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        # make the caller hang up after the first block
        real_speak = client.speak
        count = {"n": 0}

        async def flaky(text: str) -> None:
            await real_speak(text)
            count["n"] += 1
            if count["n"] == 1:
                fake.hangup()

        client.speak = flaky  # type: ignore[method-assign]
        orch = CallOrchestrator(client=client, session_factory=factory, settings=_fast_settings())
        stage = await orch.run(session_id)
    assert stage == "aborted_error"
    async with factory() as s:
        call = await s.get(CommunicationSession, session_id)
    assert call.script_stage == "aborted_error"
    assert call.needs_review is True


@pytest.mark.asyncio
async def test_operator_hangup_command(factory) -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    cmds = ["hangup"]

    async def command_check() -> str | None:
        return cmds.pop() if cmds else None

    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        orch = CallOrchestrator(client=client, session_factory=factory, settings=_fast_settings(),
                                command_check=command_check)
        stage = await orch.run(session_id)
    assert stage == "aborted_operator"
    assert fake._call_state == "IDLE"


@pytest.mark.asyncio
async def test_stop_command_plays_short_closing(factory) -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    calls = {"n": 0}

    async def command_check() -> str | None:
        calls["n"] += 1
        return "stop" if calls["n"] >= 2 else None  # trip after the greeting starts

    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        orch = CallOrchestrator(client=client, session_factory=factory, settings=_fast_settings(),
                                command_check=command_check)
        stage = await orch.run(session_id)
    assert stage == "aborted_operator"
    async with factory() as s:
        turns = (await s.scalars(
            select(CommunicationTurn).where(CommunicationTurn.session_id == session_id)
        )).all()
    assert any("прервать" in (t.spoken_text or "") for t in turns)  # short/interrupted closing used
```

(The `test_hard_cap_cuts_listening` helper above is sketchy — write it cleanly: just construct `Settings(_env_file=None, phone_auto_answer_enabled=True, ...tiny..., phone_call_hard_cap_seconds=0.3, phone_listen_silence_timeout_seconds=10.0)` directly.)

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: Implement `CallOrchestrator`**

```python
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import anyio
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CommunicationSession
from app.models.enums import CommunicationOutcome, TurnDeliveryStatus
from app.phone.client import PhoneGateClient, PhoneGateError, PhoneGateUnavailable
from app.phone.script import SCRIPT_CLOSING, SCRIPT_CLOSING_INTERRUPTED, SCRIPT_GREETING
from app.phone.sessions import SessionStore
from app.phone.speak import CallEnded, observe_tx_delivery, speak_block, wait_until_speakable
from app.settings.config import Settings

logger = structlog.get_logger(__name__)

TERMINAL_STAGES = {"greeting_completed", "aborted_operator", "aborted_error", "aborted_restart"}
_TX_START_GRACE = 1.5


class CallOrchestrator:
    def __init__(
        self,
        *,
        client: PhoneGateClient,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        command_check: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self._client = client
        self._sf = session_factory
        self._s = settings
        self._command_check = command_check
        self._store = SessionStore()
        self._session_id: UUID | None = None
        self._last_tx_transcript_id = 0

    async def run(self, session_id: UUID) -> str:
        self._session_id = session_id
        try:
            return await self._drive()
        except Exception as exc:  # noqa: BLE001 — a phone failure must never crash the process
            logger.warning("phone_orchestrator_crashed", error=type(exc).__name__)
            await self._finish("aborted_error", needs_review=True)
            return "aborted_error"

    async def _cmd(self) -> str | None:
        if self._command_check is None:
            return None
        try:
            return await self._command_check()
        except Exception:  # noqa: BLE001
            return None

    async def _set_stage(self, stage: str) -> None:
        async with self._sf() as s:
            call = await s.get(CommunicationSession, self._session_id)
            if call is not None:
                await self._store.set_script_stage(call, stage)
                await s.commit()

    async def _finish(self, stage: str, *, needs_review: bool = False) -> None:
        async with self._sf() as s:
            call = await s.get(CommunicationSession, self._session_id)
            if call is not None:
                await self._store.set_script_stage(call, stage)
                if needs_review:
                    call.needs_review = True
                await s.commit()

    async def _drive(self) -> str:
        s = self._s
        # answer
        try:
            await self._client.answer()
        except (PhoneGateUnavailable, PhoneGateError):
            logger.warning("phone_orchestrator_answer_failed")
            return "aborted_error"
        # wait for IN_CALL
        try:
            await wait_until_speakable(
                self._client,
                timeout=s.phone_answer_connect_timeout_seconds,
                poll=s.phone_orchestrator_poll_seconds,
            )
        except (CallEnded, Exception):  # noqa: BLE001
            await self._finish("aborted_error", needs_review=True)
            return "aborted_error"

        async with self._sf() as db:
            call = await db.get(CommunicationSession, self._session_id)
            if call is not None:
                await self._store.mark_auto_answered(call, datetime.now(UTC))
                await self._store.set_script_stage(call, "greeting")
                await db.commit()

        await anyio.sleep(s.phone_post_connect_wait_seconds)

        muted = False
        # GREETING
        for block in SCRIPT_GREETING:
            cmd = await self._cmd()
            if cmd == "hangup":
                return await self._abort_operator(short=False)
            if cmd == "stop":
                return await self._abort_operator(short=True)
            if cmd == "mute":
                muted = True
            if muted:
                break
            outcome = await self._say(block)
            if outcome == "ended":
                await self._finish("aborted_error", needs_review=True)
                return "aborted_error"
            await anyio.sleep(s.phone_inter_block_listen_seconds)

        # LISTENING
        await self._set_stage("listening")
        listen_start = time.monotonic()
        last_line_at = listen_start
        while True:
            cmd = await self._cmd()
            if cmd == "hangup":
                return await self._abort_operator(short=False)
            if cmd == "stop":
                return await self._abort_operator(short=True)

            try:
                status = await self._client.device_status()
                tp = await self._client.transcript(after_id=0, limit=250)
            except (PhoneGateUnavailable, PhoneGateError):
                await self._finish("aborted_error", needs_review=True)
                return "aborted_error"
            if status.call_state != "IN_CALL":
                await self._finish("aborted_error", needs_review=True)
                return "aborted_error"

            rx = [e for e in tp.entries if e.speaker == "rx" and e.timestamp_ms]
            if rx:
                newest_ms = max(e.timestamp_ms for e in rx) / 1000
                last_line_at = max(last_line_at, newest_ms)

            now = time.monotonic()
            if now - last_line_at >= s.phone_listen_silence_timeout_seconds:
                break
            if now - listen_start >= s.phone_call_hard_cap_seconds:
                break
            await anyio.sleep(s.phone_orchestrator_poll_seconds)

        # CLOSING
        await self._set_stage("closing")
        await self._say(SCRIPT_CLOSING)
        await self._hangup()
        await self._finish("greeting_completed")
        return "greeting_completed"

    async def _abort_operator(self, *, short: bool) -> str:
        if short:
            await self._set_stage("closing")
            await self._say(SCRIPT_CLOSING_INTERRUPTED)
        await self._hangup()
        await self._finish("aborted_operator")
        return "aborted_operator"

    async def _say(self, text: str) -> str:
        """speak one block, record the assistant turn, reconcile delivery.
        Returns 'ok' or 'ended'."""
        res = await speak_block(
            self._client, text,
            fence_timeout=self._s.phone_speak_fence_timeout_seconds,
            poll=self._s.phone_orchestrator_poll_seconds,
        )
        if res.outcome == "ended":
            return "ended"
        # find the tx transcript id
        tx_id: int | None = None
        try:
            tp = await self._client.transcript(after_id=self._last_tx_transcript_id, limit=250)
            tx_lines = [e for e in tp.entries if e.speaker == "tx"]
            if tx_lines:
                tx_id = tx_lines[-1].id
                self._last_tx_transcript_id = tx_id
        except (PhoneGateUnavailable, PhoneGateError):
            pass

        initial = (
            TurnDeliveryStatus.DELIVERY_UNKNOWN
            if res.outcome == "unknown"
            else TurnDeliveryStatus.ATTEMPTED
        )
        async with self._sf() as db:
            turn = await self._store.record_assistant_turn(
                db, session_id=self._session_id, phonegate_transcript_id=tx_id,
                spoken_text=text, delivery_status=initial, occurred_at=datetime.now(UTC),
            )
            turn_id = turn.id
            await db.commit()

        if res.outcome == "unknown":
            return "ok"

        result = await observe_tx_delivery(
            self._client,
            timeout=self._s.phone_speak_fence_timeout_seconds,
            poll=self._s.phone_orchestrator_poll_seconds,
            start_grace=_TX_START_GRACE,
        )
        async with self._sf() as db:
            await self._store.set_turn_delivery(
                db, turn_id=turn_id, status=TurnDeliveryStatus(result)
            )
            await db.commit()
        return "ended" if result == "failed" else "ok"

    async def _hangup(self) -> None:
        try:
            await self._client.hangup()
        except (PhoneGateUnavailable, PhoneGateError):
            logger.warning("phone_orchestrator_hangup_failed")
```

Notes for the implementer:
- `TurnDeliveryStatus(result)` works because the `observe_tx_delivery` return strings match enum *values* (`"delivered"`, `"failed"`, `"delivery_unknown"`).
- The `except (CallEnded, Exception)` after the connect fence is deliberate — a broad catch there keeps the process alive; refine to `except (CallEnded, PhoneGateError, PhoneGateUnavailable)` if mypy/ruff complains, but ensure any failure yields `aborted_error`.
- **`_say` must abort the greeting (return `"ended"`) only when the call has actually left `IN_CALL`** — from `speak_block` returning `"ended"`, or, when `observe_tx_delivery` returns `"failed"`, after a confirming `client.device_status()` shows `call_state != "IN_CALL"`. If `observe_tx_delivery` returns `"failed"` but the call is still `IN_CALL` (PhoneGate accepted the speak but TX never fired for that one block — the caller heard nothing), record the turn `FAILED` and **continue** to the next block.
- When a `"mute"` command is seen, before `break`ing the greeting loop write `mute_requested` into the session diagnostics (spec §9.3): `async with self._sf() as db: call = await db.get(CommunicationSession, self._session_id); call.diagnostics = {**call.diagnostics, "mute_requested": True}; await db.commit()`.
- The session `close` / `outcome` is still owned by `IngestLoop` (it closes on the next `IDLE`). The orchestrator only sets `script_stage` / `needs_review` / `auto_answered` / `diagnostics` / assistant turns.

- [ ] **Step 4: Run — passes** (adjust test timing constants if flaky; keep polls ≥ 0.01)

- [ ] **Step 5: Sweep + commit**

```bash
uv run pytest -q tests/unit/test_phone_orchestrator.py
uv run ruff check app/phone/orchestrator.py tests/unit/test_phone_orchestrator.py
uv run ruff format --check app/phone/orchestrator.py tests/unit/test_phone_orchestrator.py
uv run mypy app fixture_site
uv run pytest -q
git add app/phone/orchestrator.py tests/unit/test_phone_orchestrator.py
git commit -m "feat: CallOrchestrator — deterministic half-duplex greeting/listen/closing"
```

---

### Task 11: `OrchestratorSupervisor` — policy gate, spawn/monitor, runtime stop, per-call commands

**Files:**
- Modify: `app/phone/orchestrator.py` (add `OrchestratorSupervisor`)
- Test: `tests/unit/test_phone_orchestrator.py` (extend)

**Interfaces:**
- Consumes: `IngestLoop.last_status` + `.open_session_id`, `PhoneGateClient`, `Redis`, `Settings`, `should_answer` (`app.phone.policy`), `record_audit_event` (`app.audit`), `mask_phone` / `normalize_e164` (`app.phone.numbers`).
- Produces:
  ```python
  AUTO_ANSWER_STOPPED_KEY = "job-agent:phone:auto_answer_stopped"
  CALL_OWNED_KEY = "job-agent:phone:call:owned"
  CALL_CMD_KEY = "job-agent:phone:call:cmd"   # value "hangup:<sid>" | "mute:<sid>"

  class OrchestratorSupervisor:
      def __init__(self, *, client, session_factory, redis, settings) -> None
      async def tick(self, status: DeviceStatus | None, open_session_id: UUID | None) -> None
      async def shutdown(self) -> None   # cancel a running orchestrator task, clear CALL_OWNED_KEY
  ```
  `tick`: if an orchestrator task is running and not done → return. If done → await it (log), drop the ref, clear `CALL_OWNED_KEY`. Else if `status.call_state == "RINGING"` and `open_session_id`: read `AUTO_ANSWER_STOPPED_KEY`, call `should_answer(...)`, `record_audit_event("communication.auto_answer_decision", ...)`, and if `decision.answer` spawn `CallOrchestrator(...).run(open_session_id)` as an `asyncio.Task`, set `CALL_OWNED_KEY`, and pass a `command_check` that reads `CALL_CMD_KEY` (returns `"hangup"`/`"mute"` when it targets `open_session_id`, `"stop"` when `AUTO_ANSWER_STOPPED_KEY` is set, else `None`; clears a consumed `CALL_CMD_KEY`).

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_phone_orchestrator.py`)

```python
@pytest.mark.asyncio
async def test_supervisor_spawns_on_ringing_and_answers(factory) -> None:
    import asyncio

    from app.phone.orchestrator import CALL_OWNED_KEY, OrchestratorSupervisor
    from app.phone.schemas import DeviceStatus
    from tests.fixtures.fake_redis import FakeAsyncRedis

    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    redis = FakeAsyncRedis()
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        sup = OrchestratorSupervisor(client=client, session_factory=factory, redis=redis,
                                     settings=_fast_settings())
        await sup.tick(await client.device_status(), session_id)
        assert await redis.get(CALL_OWNED_KEY) == str(session_id)
        # let it run to completion
        for _ in range(500):
            await asyncio.sleep(0.01)
            await sup.tick(await client.device_status(), session_id)
            if await redis.get(CALL_OWNED_KEY) is None:
                break
        assert fake._call_state == "IDLE"
    async with factory() as s:
        call = await s.get(CommunicationSession, session_id)
    assert call.auto_answered is True


@pytest.mark.asyncio
async def test_supervisor_respects_runtime_stop(factory) -> None:
    from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY, CALL_OWNED_KEY, OrchestratorSupervisor
    from tests.fixtures.fake_redis import FakeAsyncRedis

    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    redis = FakeAsyncRedis()
    await redis.set(AUTO_ANSWER_STOPPED_KEY, "1")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        sup = OrchestratorSupervisor(client=client, session_factory=factory, redis=redis,
                                     settings=_fast_settings())
        await sup.tick(await client.device_status(), session_id)
        assert await redis.get(CALL_OWNED_KEY) is None
        assert fake._call_state == "RINGING"  # not answered


@pytest.mark.asyncio
async def test_supervisor_disabled_by_config_does_not_answer(factory) -> None:
    from app.phone.orchestrator import CALL_OWNED_KEY, OrchestratorSupervisor
    from tests.fixtures.fake_redis import FakeAsyncRedis

    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    redis = FakeAsyncRedis()
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as client:
        sup = OrchestratorSupervisor(client=client, session_factory=factory, redis=redis,
                                     settings=Settings(_env_file=None))  # auto-answer OFF
        await sup.tick(await client.device_status(), session_id)
        assert await redis.get(CALL_OWNED_KEY) is None
        assert fake._call_state == "RINGING"
```

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: Implement `OrchestratorSupervisor`**

```python
import asyncio

from redis.asyncio import Redis

from app.audit import record_audit_event
from app.phone.numbers import mask_phone, normalize_e164
from app.phone.policy import should_answer
from app.phone.schemas import DeviceStatus

AUTO_ANSWER_STOPPED_KEY = "job-agent:phone:auto_answer_stopped"
CALL_OWNED_KEY = "job-agent:phone:call:owned"
CALL_CMD_KEY = "job-agent:phone:call:cmd"


class OrchestratorSupervisor:
    def __init__(
        self,
        *,
        client: PhoneGateClient,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
    ) -> None:
        self._client = client
        self._sf = session_factory
        self._redis = redis
        self._s = settings
        self._task: asyncio.Task[str] | None = None
        self._task_session_id: UUID | None = None

    async def _runtime_stopped(self) -> bool:
        return (await self._redis.get(AUTO_ANSWER_STOPPED_KEY)) == "1"

    def _command_check_for(self, session_id: UUID) -> Callable[[], Awaitable[str | None]]:
        async def check() -> str | None:
            if await self._runtime_stopped():
                return "stop"
            raw = await self._redis.get(CALL_CMD_KEY)
            if raw and ":" in raw:
                action, sid = raw.split(":", 1)
                if sid == str(session_id) and action in {"hangup", "mute"}:
                    await self._redis.delete(CALL_CMD_KEY)
                    return action
            return None

        return check

    async def tick(self, status: DeviceStatus | None, open_session_id: UUID | None) -> None:
        if self._task is not None:
            if not self._task.done():
                return
            try:
                stage = await self._task
                logger.info("phone_orchestrator_finished", stage=stage)
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("phone_orchestrator_task_error", error=type(exc).__name__)
            self._task = None
            self._task_session_id = None
            await self._redis.delete(CALL_OWNED_KEY)
            return

        if status is None or status.call_state != "RINGING" or open_session_id is None:
            return

        stopped = await self._runtime_stopped()
        normalized = normalize_e164(status.caller_number, region=self._s.phone_caller_region)
        decision = should_answer(
            status=status, settings=self._s, runtime_stopped=stopped, normalized_caller=normalized
        )
        async with self._sf() as db:
            await record_audit_event(
                db, actor="phone-agent", action="communication.auto_answer_decision",
                entity_type="communication_session", entity_id=str(open_session_id),
                correlation_id=str(open_session_id),
                details={"answer": decision.answer, "reason": decision.reason,
                         "caller": mask_phone(status.caller_number)},
            )
            await db.commit()
        if not decision.answer:
            return

        orch = CallOrchestrator(
            client=self._client, session_factory=self._sf, settings=self._s,
            command_check=self._command_check_for(open_session_id),
        )
        self._task = asyncio.create_task(orch.run(open_session_id))
        self._task_session_id = open_session_id
        await self._redis.set(CALL_OWNED_KEY, str(open_session_id))

    async def shutdown(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        await self._redis.delete(CALL_OWNED_KEY)
```

Add `import contextlib` at the top of `orchestrator.py`.

Note: the tests drive `tick` in a manual loop because there is no real event loop cadence in a unit test. In production (Task 12) `tick` is called once per `_run_loop` iteration; the orchestrator task runs concurrently at its own poll cadence.

- [ ] **Step 4: Run — passes**; **sweep**; **commit**

```bash
uv run pytest -q tests/unit/test_phone_orchestrator.py
uv run ruff check app/phone/orchestrator.py tests/unit/test_phone_orchestrator.py
uv run ruff format --check app/phone/orchestrator.py tests/unit/test_phone_orchestrator.py
uv run mypy app fixture_site
uv run pytest -q
git add app/phone/orchestrator.py tests/unit/test_phone_orchestrator.py
git commit -m "feat: OrchestratorSupervisor — policy gate, spawn/monitor, runtime stop, per-call commands"
```

---

### Task 12: Wire the supervisor into `agent._run_loop`; restart-mid-call marking

**Files:**
- Modify: `app/phone/agent.py`
- Test: `tests/integration/test_phone_orchestrator_loop.py` (new), `tests/unit/test_phone_agent.py` (extend)

**Interfaces:**
- Consumes: `OrchestratorSupervisor`, `IngestLoop.last_status` / `.open_session_id`.
- Produces: `_run_loop` builds an `OrchestratorSupervisor` and calls `await supervisor.tick(ingest.last_status, ingest.open_session_id)` after each `ingest.run_cycle()`; calls `supervisor.shutdown()` in the `finally`. On startup, after `reconcile(status)`, if `status.call_state == "IN_CALL"` and the open session has `auto_answered` true and `script_stage not in TERMINAL_STAGES` → set `script_stage = "aborted_restart"`, `needs_review = true`.

- [ ] **Step 1: Write the failing integration test**

`tests/integration/test_phone_orchestrator_loop.py`:

```python
from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import TurnSpeaker
from app.phone import agent as agent_module
from app.settings.config import Settings
from tests.fixtures.fake_phonegate import FakePhoneGate
from tests.fixtures.fake_redis import FakeAsyncRedis


@pytest_asyncio.fixture
async def profiled_factory(sqlite_session_factory: async_sessionmaker[AsyncSession]):
    async with sqlite_session_factory() as s:
        s.add(UserProfile(name="d", is_default=True))
        await s.commit()
    return sqlite_session_factory


def _settings() -> Settings:
    return Settings(
        _env_file=None, phone_agent_enabled=True, phonegate_auth_token="tok",
        phone_auto_answer_enabled=True,
        phone_poll_idle_seconds=0.02, phone_poll_active_seconds=0.02,
        phone_post_connect_wait_seconds=0.01, phone_speak_fence_timeout_seconds=2.0,
        phone_inter_block_listen_seconds=0.01, phone_listen_silence_timeout_seconds=0.2,
        phone_call_hard_cap_seconds=5.0, phone_orchestrator_poll_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_agent_auto_answers_and_runs_the_script(
    monkeypatch: pytest.MonkeyPatch, profiled_factory: async_sessionmaker[AsyncSession]
) -> None:
    fake = FakePhoneGate()
    redis = FakeAsyncRedis()

    class _RedisMod:
        @staticmethod
        def from_url(*a: Any, **k: Any) -> FakeAsyncRedis:
            return redis

    monkeypatch.setattr(agent_module, "AsyncRedis", _RedisMod)
    monkeypatch.setattr(agent_module, "PhoneGateClient",
                        lambda **kw: __import__("app.phone.client", fromlist=["PhoneGateClient"]).PhoneGateClient(
                            base_url="http://pg", token="t", transport=fake.transport()))
    monkeypatch.setattr(agent_module, "async_session_factory", profiled_factory)
    monkeypatch.setattr(agent_module, "get_settings", _settings)

    async def _drive() -> None:
        task = asyncio.create_task(agent_module._run_loop(lease_lost=lambda: False))
        await asyncio.sleep(0.05)
        fake.ring("+37360111222")
        # a caller turn, then silence -> the loop should answer + greet + close
        await asyncio.sleep(0.2)
        fake.transcript(speaker="rx", text="Звоню по вакансии грузчика")
        for _ in range(400):
            await asyncio.sleep(0.02)
            if fake._call_state == "IDLE" and any(e["type"] == "call_state" for e in fake._events):
                # wait until the session is closed
                async with profiled_factory() as s:
                    call = (await s.scalars(select(CommunicationSession))).first()
                if call is not None and call.script_stage == "greeting_completed":
                    break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await _drive()

    async with profiled_factory() as s:
        call = (await s.scalars(select(CommunicationSession))).one()
        turns = (await s.scalars(select(CommunicationTurn))).all()
    assert call.auto_answered is True
    assert call.script_stage == "greeting_completed"
    assert any(t.speaker is TurnSpeaker.ASSISTANT for t in turns)
    assert any(t.speaker is TurnSpeaker.EMPLOYER and "грузчика" in t.text for t in turns)
```

(This test is timing-heavy — if it proves flaky in CI, mark it `@pytest.mark.slow` and keep the deterministic coverage in `test_phone_orchestrator.py`. Do not delete it; it is the only test that exercises `IngestLoop` + supervisor + orchestrator together.)

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: Implement the wiring in `app/phone/agent.py`**

In `_run_loop`, after building `ingest`:

```python
    from app.phone.orchestrator import TERMINAL_STAGES, OrchestratorSupervisor

    supervisor = OrchestratorSupervisor(
        client=client, session_factory=async_session_factory, redis=async_redis, settings=settings
    )
```

In the startup `else:` branch, after `await ingest.reconcile(status)`:

```python
            if status.call_state == "IN_CALL":
                async with async_session_factory() as db:
                    open_row = await ingest._store.find_open(db)
                    if (
                        open_row is not None
                        and open_row.auto_answered
                        and (open_row.script_stage or "") not in TERMINAL_STAGES
                    ):
                        open_row.script_stage = "aborted_restart"
                        open_row.needs_review = True
                        await db.commit()
```

In the `while not should_stop():` loop, after the orphan-close block and before `_touch_heartbeat()`:

```python
            await supervisor.tick(ingest.last_status, ingest.open_session_id)
```

In the `finally:` block, before `await client.aclose()`:

```python
        await supervisor.shutdown()
```

- [ ] **Step 4: Run — passes**; **sweep**; **commit**

```bash
uv run pytest -q tests/integration/test_phone_orchestrator_loop.py tests/unit/test_phone_agent.py
uv run ruff check app/phone/agent.py tests/integration/test_phone_orchestrator_loop.py
uv run ruff format --check app/phone/agent.py tests/integration/test_phone_orchestrator_loop.py
uv run mypy app fixture_site
uv run pytest -q
git add app/phone/agent.py tests/integration/test_phone_orchestrator_loop.py
git commit -m "feat: wire OrchestratorSupervisor into the phone-agent loop; restart-mid-call marking"
```

---

### Task 13: API — `auto_answer` block in `GET /api/v1/phone/status`

**Files:**
- Modify: `app/api/phone_routes.py`
- Test: `tests/integration/test_phone_api.py`

**Interfaces:**
- Consumes: `AUTO_ANSWER_STOPPED_KEY` / `CALL_OWNED_KEY` (`app.phone.orchestrator`), `settings.phone_auto_answer_enabled`, session `auto_answered` / `script_stage`.
- Produces: `phone_status()` response gains `"auto_answer": {"enabled": bool, "stopped": bool, "last_decision": {...} | None}` and `current_call` gains `"auto_answered": bool`, `"script_stage": str | None`.

- [ ] **Step 1: Write the failing test**

`tests/integration/test_phone_api.py` — add:

```python
@pytest.mark.asyncio
async def test_status_auto_answer_block(client, sqlite_session_factory) -> None:
    async with sqlite_session_factory() as s:
        profile = UserProfile(name="d", is_default=True)
        s.add(profile)
        await s.flush()
        s.add(CommunicationSession(
            profile_id=profile.id, channel=CommunicationChannel.CALL, transport="phonegate",
            direction=CommunicationDirection.INBOUND, remote_address="+37360111222",
            remote_raw="+37360111222", phonegate_event_id_start=1,
            started_at=datetime.now(UTC), answered_at=datetime.now(UTC),
            auto_answered=True, script_stage="listening",
        ))
        await s.commit()

    body = (await client.get("/api/v1/phone/status")).json()
    assert body["auto_answer"]["enabled"] in (True, False)
    assert body["auto_answer"]["stopped"] is False
    assert body["current_call"]["auto_answered"] is True
    assert body["current_call"]["script_stage"] == "listening"
```

- [ ] **Step 2: Run — fails** (`KeyError: 'auto_answer'`)

- [ ] **Step 3: Implement**

In `app/api/phone_routes.py::phone_status`, the endpoint needs Redis. Add a dependency:

```python
from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY

# ... inside phone_status, after computing device_block:
from redis.asyncio import Redis as AsyncRedis

redis = AsyncRedis.from_url(get_settings().redis_url, decode_responses=True)
try:
    stopped = (await redis.get(AUTO_ANSWER_STOPPED_KEY)) == "1"
finally:
    await redis.aclose()
```

(There is no shared async Redis in the API process; a short-lived connection per call is fine — this endpoint is admin/diagnostic, not hot. If the repo already has an async-redis dependency helper, use it.)

Change `current_call` construction to include the new fields (all three branches):

```python
    def _call_block(state: str, sess: CommunicationSession | None) -> dict[str, Any]:
        return {
            "state": state,
            "session_id": str(sess.id) if sess else None,
            "caller_number": mask_phone(sess.remote_address) if sess else None,
            "auto_answered": bool(sess.auto_answered) if sess else False,
            "script_stage": sess.script_stage if sess else None,
        }
```

and use it for the three cases (`idle` with `sess=None`, `connected`, `ringing`).

Add to the returned dict:

```python
        "auto_answer": {
            "enabled": get_settings().phone_auto_answer_enabled,
            "stopped": stopped,
            "last_decision": None,
        },
```

(`last_decision` is left `None` in 2a — the decision is in the audit log; wiring a Redis mirror is not worth it. The spec's §9.4 example shows it populated; note in the task report that it is deferred as `None` and update spec §16 if the operator wants it.)

- [ ] **Step 4: Run — passes**; **sweep**; **commit**

```bash
uv run pytest -q tests/integration/test_phone_api.py
uv run ruff check app/api/phone_routes.py tests/integration/test_phone_api.py
uv run ruff format --check app/api/phone_routes.py tests/integration/test_phone_api.py
uv run mypy app fixture_site
uv run pytest -q
git add app/api/phone_routes.py tests/integration/test_phone_api.py
git commit -m "feat: phone /status exposes auto-answer state and per-call script stage"
```

---

### Task 14: Admin controls — stop/resume, per-call hangup/mute, panel

**Files:**
- Modify: `app/admin/routes.py` (`_phone_health` + 4 new POST routes)
- Modify: `app/admin/templates/_phone_health.html`
- Test: `tests/unit/test_admin_ui.py`

**Interfaces:**
- Consumes: `AUTO_ANSWER_STOPPED_KEY` / `CALL_OWNED_KEY` / `CALL_CMD_KEY` (`app.phone.orchestrator`); the admin action pattern (`require_admin`, `require_csrf`, `_audit_admin`, `RedirectResponse(status_code=303)`).
- Produces: routes `POST /admin/phone/auto-answer/stop`, `POST /admin/phone/auto-answer/resume`, `POST /admin/phone/call/{session_id}/hangup`, `POST /admin/phone/call/{session_id}/mute`. `_phone_health()` return dict gains `"auto_answer": {"enabled", "stopped"}` and `"active_call": {"session_id", "script_stage"} | None`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_admin_ui.py` — add (near the other `_phone_health` tests):

```python
@pytest.mark.asyncio
async def test_phone_health_auto_answer_block(sqlite_session_factory: Any, monkeypatch) -> None:
    from app.admin import routes as admin_routes
    from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY
    from tests.fixtures.fake_redis import FakeAsyncRedis

    redis = FakeAsyncRedis()
    await redis.set(AUTO_ANSWER_STOPPED_KEY, "1")
    monkeypatch.setattr(admin_routes, "_phone_redis", lambda: redis)

    async with sqlite_session_factory() as session:
        result = await admin_routes._phone_health(session)
    assert result["auto_answer"]["stopped"] is True
    assert "enabled" in result["auto_answer"]
```

Also add an HTTP-level test that `POST /admin/phone/auto-answer/stop` (with CSRF, logged-in admin) sets the key — follow the pattern of the existing `set_pause` route test in the file (search for `admin/pause`).

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: Implement**

`app/admin/routes.py`:

- A tiny helper (module level, so tests can monkeypatch):
  ```python
  def _phone_redis() -> Any:
      from redis.asyncio import Redis as AsyncRedis

      return AsyncRedis.from_url(get_settings().redis_url, decode_responses=True)
  ```
- In `_phone_health`, after building the return dict, add keys:
  ```python
      from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY, CALL_OWNED_KEY

      redis = _phone_redis()
      try:
          stopped = (await redis.get(AUTO_ANSWER_STOPPED_KEY)) == "1"
          owned = await redis.get(CALL_OWNED_KEY)
      finally:
          await redis.aclose()
      active_call = None
      if owned:
          from app.models.entities import CommunicationSession

          call = await session.get(CommunicationSession, UUID(owned))
          if call is not None and call.ended_at is None:
              active_call = {"session_id": owned, "script_stage": call.script_stage}
      # add to the returned dict:
      #   "auto_answer": {"enabled": get_settings().phone_auto_answer_enabled, "stopped": stopped},
      #   "active_call": active_call,
  ```
  (Add `from uuid import UUID` if not already imported in `routes.py`.)
- Four routes near `set_pause` (~line 1584):
  ```python
  @router.post("/admin/phone/auto-answer/{action}")
  async def phone_auto_answer_toggle(
      action: str,
      request: Request,
      csrf_token: str = Form(...),
      _: str = Depends(require_admin),
      session: AsyncSession = Depends(get_session),
  ) -> RedirectResponse:
      require_csrf(request, csrf_token)
      if action not in {"stop", "resume"}:
          raise HTTPException(status_code=404)
      from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY

      redis = _phone_redis()
      try:
          if action == "stop":
              await redis.set(AUTO_ANSWER_STOPPED_KEY, "1")
          else:
              await redis.delete(AUTO_ANSWER_STOPPED_KEY)
      finally:
          await redis.aclose()
      await _audit_admin(session, f"phone.auto_answer.{action}", "phone_channel", "auto_answer")
      return RedirectResponse("/?view=diagnostics", status_code=303)


  @router.post("/admin/phone/call/{session_id}/{action}")
  async def phone_call_action(
      session_id: UUID,
      action: str,
      request: Request,
      csrf_token: str = Form(...),
      _: str = Depends(require_admin),
      session: AsyncSession = Depends(get_session),
  ) -> RedirectResponse:
      require_csrf(request, csrf_token)
      if action not in {"hangup", "mute"}:
          raise HTTPException(status_code=404)
      from app.phone.orchestrator import CALL_CMD_KEY, CALL_OWNED_KEY

      redis = _phone_redis()
      try:
          owned = await redis.get(CALL_OWNED_KEY)
          if owned != str(session_id):
              raise HTTPException(status_code=409, detail="not the active call")
          await redis.set(CALL_CMD_KEY, f"{action}:{session_id}")
      finally:
          await redis.aclose()
      await _audit_admin(session, f"phone.call.{action}", "communication_session", str(session_id))
      return RedirectResponse("/?view=diagnostics", status_code=303)
  ```
  Confirm `HTTPException` is imported in `routes.py` (it is used elsewhere).

`app/admin/templates/_phone_health.html` — add after the `<ul class="component-list">…</ul>`:

```html
  {% if phone_health.auto_answer %}
  <div class="phone-auto-answer">
    <span class="badge badge-{{ 'success' if phone_health.auto_answer.enabled else 'muted' }}">
      Авто-ответ: {{ 'вкл' if phone_health.auto_answer.enabled else 'выкл' }}</span>
    {% if phone_health.auto_answer.stopped %}
    <span class="badge badge-danger">остановлен оператором</span>
    <form method="post" action="/admin/phone/auto-answer/resume">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="btn btn-sm">Возобновить</button></form>
    {% else %}
    <form method="post" action="/admin/phone/auto-answer/stop"
          data-confirm-title="Остановить авто-ответ?" data-confirm="Новые звонки не будут подниматься. Текущий активный звонок будет завершён." data-confirm-tone="danger" data-confirm-action="Остановить">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="btn btn-sm btn-danger">Остановить</button></form>
    {% endif %}
  </div>
  {% endif %}
  {% if phone_health.active_call %}
  <div class="phone-active-call muted">
    Активный звонок · сценарий: {{ phone_health.active_call.script_stage or '—' }}
    <form method="post" action="/admin/phone/call/{{ phone_health.active_call.session_id }}/hangup" style="display:inline">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="btn btn-sm btn-danger">Завершить</button></form>
    <form method="post" action="/admin/phone/call/{{ phone_health.active_call.session_id }}/mute" style="display:inline">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="btn btn-sm">Замолчать</button></form>
  </div>
  {% endif %}
```

Check how `csrf_token` reaches this partial — it is included from `dashboard_diagnostics.html`; grep for `csrf_token` in the diagnostics render context (`app/admin/routes.py` ~line 1372 issues a token). If the diagnostics view does not pass `csrf_token`, add it to the template context where `phone_health` is passed (~line 1399).

- [ ] **Step 4: Run — passes**; **sweep**; **commit**

```bash
uv run pytest -q tests/unit/test_admin_ui.py
uv run ruff check app/admin/routes.py tests/unit/test_admin_ui.py
uv run ruff format --check app/admin/routes.py tests/unit/test_admin_ui.py
uv run mypy app fixture_site
uv run pytest -q
git add app/admin/routes.py app/admin/templates/_phone_health.html tests/unit/test_admin_ui.py
git commit -m "feat: admin auto-answer stop/resume + per-call hangup/mute controls"
```

---

### Task 15: Real-call harness `tests/realcall/`

**Files:**
- Create: `tests/realcall/__init__.py`, `tests/realcall/conftest.py`, `tests/realcall/a06_originate.py`, `tests/realcall/test_realcall_phase_2a.py`
- Create: `tests/unit/test_realcall_preconditions.py`
- Modify: `pyproject.toml` (register the `realcall` marker) — check the `[tool.pytest.ini_options]` `markers` list; add `"realcall: opt-in tests that place real GSM calls (never in CI)"`.

**Interfaces:**
- Consumes: nothing from `app/` at import time except `PhoneGateClient` and `Settings`; SSH/ADB via `subprocess`.
- Produces: `tests/realcall/a06_originate.py` with `class A06Rig` — `check_preconditions() -> list[str]` (empty = OK), `dial(number: str)`, `start_downlink_recording() -> str` (path), `stop_downlink_recording() -> bytes`, `inject_uplink_wav(path: str)`, `hangup()`. All via `ssh` + `adb` shelling to the VPS. `conftest.py` fixture `a06_rig` that skips the whole module with the joined precondition failures if any.

- [ ] **Step 1: `pyproject.toml` marker + the failing precondition unit test**

`tests/unit/test_realcall_preconditions.py`:

```python
from __future__ import annotations

from tests.realcall.a06_originate import A06Rig


def test_preconditions_return_reasons_when_ssh_fails(monkeypatch) -> None:
    def _boom(*a, **k):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr("subprocess.run", _boom)
    rig = A06Rig(ssh_host="x", ssh_port="1", ssh_user="u", a14_serial="", a06_serial="",
                 a06_number="060", phonegate_url="http://x", phonegate_token="t")
    reasons = rig.check_preconditions()
    assert reasons and any("ssh" in r.lower() for r in reasons)
```

- [ ] **Step 2: Run — fails** (module missing)

- [ ] **Step 3: Implement `tests/realcall/a06_originate.py`**

A self-contained wrapper. Key points, adapt from `/srv/phonegate/WORKING_DO_NOT_TOUCH_PROVEN/a06_call_record.py` (do NOT edit that file):

```python
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

import httpx


@dataclass
class A06Rig:
    ssh_host: str
    ssh_port: str
    ssh_user: str
    a14_serial: str  # e.g. "100.106.163.104:43369"; "" -> auto-detect
    a06_serial: str  # e.g. "100.100.224.9:38557"
    a06_number: str  # A14 dials this to reach A06; here A06 dials A14 -> we need A14's number
    a14_number: str = ""
    phonegate_url: str = ""
    phonegate_token: str = ""

    def _ssh(self, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["ssh", "-p", self.ssh_port, f"{self.ssh_user}@{self.ssh_host}", cmd],
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    def _adb(self, serial: str, cmd: str, timeout: int = 25) -> str:
        return self._ssh(f"adb -s {serial} shell '{cmd}'", timeout).stdout.strip()

    def check_preconditions(self) -> list[str]:
        reasons: list[str] = []
        try:
            devs = self._ssh("adb devices -l", timeout=15)
        except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
            return [f"ssh/adb unreachable: {exc}"]
        if devs.returncode != 0:
            reasons.append(f"adb devices failed: {devs.stderr.strip()[:200]}")
        if self.a14_serial and self.a14_serial not in devs.stdout:
            reasons.append(f"A14 {self.a14_serial} not in adb devices")
        if self.a06_serial and self.a06_serial not in devs.stdout:
            reasons.append(f"A06 {self.a06_serial} not in adb devices")
        if self.phonegate_url and self.phonegate_token:
            try:
                r = httpx.get(f"{self.phonegate_url}/api/device/status",
                              headers={"Authorization": f"Bearer {self.phonegate_token}"}, timeout=10)
                st = r.json()
                if not st.get("connected") or st.get("mode") != "Zero-ADB":
                    reasons.append(f"PhoneGate not ready: connected={st.get('connected')} mode={st.get('mode')}")
            except (httpx.HTTPError, ValueError) as exc:
                reasons.append(f"PhoneGate status unreachable: {exc}")
        if not self.a14_number:
            reasons.append("a14_number not configured (A06 needs it to dial A14)")
        return reasons

    def dial(self, number: str) -> None:
        self._adb(self.a06_serial, f"am start -a android.intent.action.CALL -d tel:{number}")

    def hangup(self) -> None:
        self._adb(self.a06_serial, "input keyevent KEYCODE_ENDCALL")

    def inject_uplink_wav(self, remote_wav_path: str) -> None:
        # Uses the proven CallStreamer/ParamSetter primitives; the exact invocation
        # mirrors WORKING_DO_NOT_TOUCH_PROVEN/a14_call_inject.py adapted for A06.
        # IMPLEMENTER: fill in from that reference during the real-hardware bring-up.
        raise NotImplementedError("uplink injection — wire from the proven toolkit on the rig")

    def start_downlink_recording(self) -> str:
        # ReceiverRecorder src 3 (VOICE_DOWNLINK) on A06, mirroring a06_call_record.py.
        raise NotImplementedError("downlink recording — wire from the proven toolkit on the rig")

    def stop_downlink_recording(self) -> bytes:
        raise NotImplementedError("downlink recording — wire from the proven toolkit on the rig")
```

The three `NotImplementedError` methods are the pieces that can only be finalized against the physical rig (the proven scripts do A14→A06; the reverse direction's exact `adb`/`app_process` invocations must be confirmed live). **This is expected** — the task deliverable is the harness skeleton + precondition gating + the operator runbook; the operator completes the injection/recording wiring during the real-hardware bring-up as part of the 2a merge gate. Document this explicitly in `tests/realcall/README.md` (create it).

- [ ] **Step 4: Implement `tests/realcall/conftest.py`**

```python
from __future__ import annotations

import os

import pytest

from tests.realcall.a06_originate import A06Rig


@pytest.fixture(scope="module")
def a06_rig() -> A06Rig:
    if os.getenv("ENABLE_REALCALL_TESTS") != "true":
        pytest.skip("real-call tests are opt-in: set ENABLE_REALCALL_TESTS=true")
    rig = A06Rig(
        ssh_host=os.getenv("REALCALL_SSH_HOST", "46.225.103.75"),
        ssh_port=os.getenv("REALCALL_SSH_PORT", "39637"),
        ssh_user=os.getenv("REALCALL_SSH_USER", "andrei"),
        a14_serial=os.getenv("REALCALL_A14_SERIAL", ""),
        a06_serial=os.getenv("REALCALL_A06_SERIAL", ""),
        a06_number=os.getenv("REALCALL_A06_NUMBER", ""),
        a14_number=os.getenv("REALCALL_A14_NUMBER", ""),
        phonegate_url=os.getenv("PHONEGATE_URL", ""),
        phonegate_token=os.getenv("PHONEGATE_AUTH_TOKEN", ""),
    )
    reasons = rig.check_preconditions()
    if reasons:
        pytest.skip("real-call preconditions not met:\n  - " + "\n  - ".join(reasons))
    return rig
```

- [ ] **Step 5: Implement `tests/realcall/test_realcall_phase_2a.py`** (the scenarios from spec §12.2)

Write `test_realcall_greeting_and_capture`, `test_realcall_runtime_stop_aborts`, `test_realcall_per_call_hangup`, `test_realcall_disabled_is_observed_only` per spec §12.2. Each: use `a06_rig` to dial A14, drive the injection/recording, then assert against the JobHunter DB (`CommunicationSession` / `CommunicationTurn`) via a direct async engine to the DEV Postgres, and against the recorded downlink via Faster-Whisper (import guarded — skip if `faster_whisper` is not installed). Mark all `@pytest.mark.realcall`.

- [ ] **Step 6: Create `tests/realcall/README.md`** — the operator runbook:

```markdown
# Real-call harness (Phase 2a merge gate)

`pytest -m realcall` places REAL GSM calls A06 -> A14. Never runs in CI.

## Prerequisites
- SSH to the VPS reachable; `adb` there sees A14 and A06 over Tailscale.
- A06: active SIM with minutes, screen unlocked, proven `.dex` present.
- A14: PhoneGate running, daemon connected (`connected=true`, `mode=Zero-ADB`).
- JobHunter `call-agent` running against the same PhoneGate with
  `PHONE_AUTO_ANSWER_ENABLED=true`.

## Run
    ENABLE_REALCALL_TESTS=true \
    PHONEGATE_URL=https://phonegate.46-225-103-75.sslip.io \
    PHONEGATE_AUTH_TOKEN=... \
    REALCALL_A14_NUMBER=... REALCALL_A06_NUMBER=... \
    REALCALL_A14_SERIAL=... REALCALL_A06_SERIAL=... \
    uv run pytest -q -m realcall tests/realcall/

## 2a "done" checklist
- [ ] `pytest -m realcall` green against the live A14 gate
- [ ] Manual acceptance call passed (operator calls A14, hears the greeting,
      speaks, hears the closing, call ends — sounds acceptable to a real employer)
- [ ] Full CI sweep green (ruff, mypy, pytest, alembic check on DEV Postgres, docker compose config)

## Incomplete pieces to finish on the rig
`a06_originate.py` `inject_uplink_wav` / `start_downlink_recording` / `stop_downlink_recording`
raise NotImplementedError — wire them from
`/srv/phonegate/WORKING_DO_NOT_TOUCH_PROVEN/` (a06_call_record.py, a14_call_inject.py,
CallStreamer, ParamSetter, ReceiverRecorder) for the A06->A14 direction. Do NOT edit
that protected directory.
```

- [ ] **Step 7: Run the unit precondition test + full sweep**

```bash
uv run pytest -q tests/unit/test_realcall_preconditions.py
uv run pytest -q  # tests/realcall/ is collected but every test skips without ENABLE_REALCALL_TESTS
uv run ruff check tests/realcall tests/unit/test_realcall_preconditions.py
uv run ruff format --check tests/realcall tests/unit/test_realcall_preconditions.py
uv run mypy app fixture_site
```

- [ ] **Step 8: Commit**

```bash
git add tests/realcall tests/unit/test_realcall_preconditions.py pyproject.toml
git commit -m "test: real-call harness for phase 2a (opt-in, gates the merge)"
```

---

## Self-review

**1. Spec coverage:**

| Spec §1.2 item | Task |
|---|---|
| `PhoneGateClient.answer/speak/hangup` | 4 |
| `speak.py` fence + `/speak` handling | 7 |
| `policy.py` `should_answer` | 6 |
| `orchestrator.py` `CallOrchestrator` | 10 |
| `script.py` phrase blocks | 5 |
| migration: turn `delivery_status`/`spoken_text`, session `auto_answered`/`script_stage`, `TurnDeliveryStatus` | 1 |
| settings `phone_auto_answer_enabled`, `phone_answer_blocklist`, timings | 2 |
| runtime stop: Redis key + `POST /admin/phone/auto-answer/{stop\|resume}` | 11 (key), 14 (routes) |
| per-call `POST /admin/phone/call/{id}/{hangup\|mute}` | 14 (routes), 10/11 (command channel + orchestrator handling) |
| `GET /api/v1/phone/status` `auto_answer` block + per-call `auto_answered`/`script_stage` | 13 |
| admin panel "Авто-ответ" block + active-call buttons | 14 |
| `FakePhoneGate` `answer/speak/hangup` + TX sim | 3 |
| unit + integration on FakePhoneGate | 3,4,6,7,8,9,10,11,12,13,14 |
| real-call harness `tests/realcall/` (opt-in, never CI) | 15 |
| manual acceptance one-time | 15 (README runbook) |
| spec §8 process architecture (`IngestLoop` slow owner + `CallOrchestrator` fast per-call) | 9 (`last_status`, skip tx), 11 (supervisor), 12 (wiring) |
| spec §10 failure/restart table | 10 (answer-fail, `/speak` ambiguous, call drop, PhoneGate unreachable, orchestrator crash), 12 (restart-mid-call `aborted_restart`) |
| spec §13 settings table | 2 |

Gap: spec §9.4's `last_decision` in `/status` is deferred to `None` (Task 13 note — the decision lives in the audit log; the task report flags it and spec §16 lets the operator ask for a Redis mirror). `mute_requested` diagnostics (spec §9.3) is covered by the Task 10 implementer note.

**2. Placeholder scan:** the three `NotImplementedError` methods in `a06_originate.py` (Task 15) are deliberate and documented as the physical-rig bring-up step that the operator completes as part of the merge gate — not a plan gap. Everything else has concrete code.

**3. Type consistency:** `TurnDeliveryStatus` values (`"delivered"`/`"failed"`/`"delivery_unknown"`) match `observe_tx_delivery`'s return strings so `TurnDeliveryStatus(result)` is valid (Task 7 ↔ 10). `SpeakResult.outcome` ∈ `{"ok","ended","unknown"}` (Task 7) consumed in `CallOrchestrator._say` (Task 10). `AnswerDecision.reason` strings (Task 6) audited verbatim in the supervisor (Task 11). `script_stage` strings (`greeting`/`listening`/`closing`/`greeting_completed`/`aborted_operator`/`aborted_error`/`aborted_restart`) and `TERMINAL_STAGES` are consistent across Tasks 10, 11, 12. Redis key constants (`AUTO_ANSWER_STOPPED_KEY`, `CALL_OWNED_KEY`, `CALL_CMD_KEY`) defined once in `orchestrator.py` (Task 11) and imported by Tasks 12, 13, 14. `IngestLoop.last_status` (Task 9) consumed by Task 12.

## Execution Handoff
