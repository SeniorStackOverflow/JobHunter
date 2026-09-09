# Phone Call Agent Phase 2b Production Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish Phase 2b as a Russian-only, production-usable post-call verification system with conservative critical-field handling, inbound SMS confirmation, audited review controls, and real DEV validation.

**Architecture:** The deterministic realtime call path keeps the existing greeting/listen/closing flow and only selects between two fixed closings. Celery owns a versioned post-call pipeline: independent Extractor and Verifier llmRouter calls, deterministic reconciliation, an Arbiter call for critical facts, `CallFact` persistence, SMS ingestion/reprocessing, and Telegram delivery. The admin UI and REST API read the same persisted trust state and expose short evidence clips plus audited correction controls.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, httpx, Celery/Redis, PostgreSQL 16, SQLite test migrations, Jinja, pytest/pytest-asyncio, Playwright, llmRouter, PhoneGate, Telegram Bot API.

**Spec:** `docs/superpowers/specs/2026-09-06-phone-call-agent-phase-2b-production-readiness-design.md`

## Global Constraints

- Russian is the only supported call language in Phase 2b.
- Realtime dialogue remains deterministic; no LLM call may run in `CallOrchestrator` or `IngestLoop`.
- Post-call work runs only in Celery.
- Critical facts never become `confirmed` from model confidence; only correlated SMS or an audited operator action can confirm them.
- `candidate` is displayed as `high_confidence` only after independent agreement, quote validation, deterministic normalization, Arbiter acceptance, evidence availability, and no contradiction.
- Relative dates use the call timestamp and `Europe/Chisinau`.
- No `InterviewAppointment` or calendar write, outbound call, or outbound SMS is added.
- PhoneGate source stays unchanged; JobHunter may call `GET /api/sms` and `POST /api/sms/sync`.
- Caller numbers stay masked in lists, REST responses, Telegram, logs, and audit details.
- Raw employer transcript and SMS text never enter application logs.
- Existing evidence storage and retention limits remain enforced.
- Every Pydantic model sent as a strict llmRouter schema uses `ConfigDict(extra="forbid")`.
- Each model pass persists provider, model, latency, attempts, pipeline version, and sanitized failures.
- Tests never use live PhoneGate, llmRouter, Telegram, GSM, or production services unless explicitly marked opt-in.
- Never restart or modify `/srv/phonegate` while implementing this plan.
- Never touch `/srv/jobhunter-prod` or the production Compose project.
- Required final checks: `ruff check .`, `ruff format --check .`, `mypy app fixture_site`, full `pytest`, DEV PostgreSQL Alembic upgrade/check, DEV Compose, real integrations, and three consecutive admin Playwright runs.
- Preserve unrelated working-tree changes. At plan start these are the evidence-stream race fix in `app/admin/phone_routes.py` and its test in `tests/unit/test_phone_admin_calls.py`.
- Implementation agent: `gpt-5.6-luna`. Per-task reviewer: `gpt-5.6-terra`. Whole-branch reviewer: `gpt-5.6-sol`. Escalate complex unresolved correctness issues to `gpt-6-astra`.
- Every implementation task uses TDD, ends in a focused commit, then passes a Terra spec-compliance and code-quality review before dependent work begins.

---

## File Structure

**Create:**

- `app/phone/critical.py` — Russian marker detection and deterministic normalization.
- `app/phone/verification.py` — strict llmRouter schemas, transport, and the Extractor/Verifier/Arbiter/SMS calls.
- `app/phone/reconciliation.py` — pure quote validation, agreement, and trust-state decisions.
- `app/phone/facts.py` — current-fact upsert and verification document persistence.
- `app/phone/sms.py` — PhoneGate SMS ingestion, idempotency, correlation, and call reprocessing.
- `migrations/versions/<revision>_phone_phase_2b_verification_sms.py` — additive production-readiness schema.
- `tests/unit/test_phone_critical.py`
- `tests/unit/test_phone_verification.py`
- `tests/unit/test_phone_reconciliation.py`
- `tests/unit/test_phone_facts.py`
- `tests/unit/test_phone_sms.py`
- `tests/integration/test_phone_verification_e2e.py`
- `tests/integration/test_phone_admin_review.py`
- `tests/realcall/test_realcall_phase_2b.py`

**Modify:**

- `app/models/enums.py` — processing and verification enums.
- `app/models/entities.py` — persisted verification/SMS linkage and fact confirmation metadata.
- `app/phone/schemas.py` — PhoneGate SMS wire schemas.
- `app/phone/client.py` — SMS history and sync methods.
- `app/phone/script.py` — fixed SMS-request closing.
- `app/phone/orchestrator.py` — deterministic closing selection and evidence exception boundary.
- `app/phone/summary.py` — context fix, strict summary compatibility, atomic pipeline coordinator.
- `app/phone/telegram.py` — status-aware rendering and delivery result.
- `app/phone/sessions.py` — verification state initialization.
- `app/settings/config.py` and `.env.example` — typed pipeline/SMS/retry settings.
- `app/scheduler/tasks.py` and `app/scheduler/celery_app.py` — SMS and Telegram jobs.
- `app/admin/phone_routes.py` — filters, detail context, manual review and SMS association endpoints.
- `app/admin/templates/_calls_history.html` and `_calls_history_detail.html` — trust-state UI and review forms.
- `app/api/phone_routes.py` — completed Phase 2b fields and filters.
- `tests/fixtures/fake_phonegate.py` — SMS history/sync behavior.
- Existing phone unit, migration, API, scheduler, admin, and real-call tests.
- `docker-compose.yml`, `docker-compose.prod.yml`, `docs/security.md`, and the Phase 2b operational documentation.

---

### Task 1: Stabilize the existing Phase 2b baseline and finish its REST surface

**Files:**

- Modify: `app/admin/phone_routes.py`
- Modify: `tests/unit/test_phone_admin_calls.py`
- Modify: `app/phone/evidence.py`
- Modify: `app/phone/orchestrator.py`
- Modify: `app/phone/summary.py`
- Modify: `app/api/phone_routes.py`
- Modify: `tests/unit/test_phone_evidence.py`
- Modify: `tests/unit/test_phone_summary.py`
- Modify: `tests/integration/test_phone_api.py`
- Create: `tests/integration/test_phone_evidence_e2e.py`

**Interfaces:**

- Produces a complete pre-extension Phase 2b baseline.
- `EvidenceCapturer.maybe_capture(entries)` never propagates any exception.
- `CallSummary` rejects unknown keys.
- `GET /api/v1/phone/sessions` accepts `summary_state`, `needs_review`, and `outcome` filters.
- List/detail responses include `summary`, `summary_state`, `script_stage`, `auto_answered`, and evidence metadata.

- [ ] **Step 1: Preserve and verify the existing evidence-file race fix**

Run:

~~~bash
uv run pytest tests/unit/test_phone_admin_calls.py -q
~~~

Expected: the test for deletion between `is_file()` and `read_bytes()` passes and returns HTTP 404.

- [ ] **Step 2: Write failing strict-summary and context tests**

Add tests equivalent to:

~~~python
def test_call_summary_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CallSummary.model_validate({"summary_text": "Итог", "unexpected": True})


def test_summary_body_includes_confirmed_profile_facts() -> None:
    provider = _provider()
    body = provider._body(
        CallSummaryContext(
            transcript=[("employer", "Звоню по вакансии")],
            confirmed_facts={"confirmed_facts": [{"field": "name", "value": "Андрей"}]},
        )
    )
    assert "Подтверждённые данные кандидата" in body["messages"][1]["content"]
    assert "Андрей" in body["messages"][1]["content"]
~~~

Run: `uv run pytest tests/unit/test_phone_summary.py -q`
Expected: FAIL because extras are accepted and confirmed facts are absent from the prompt.

- [ ] **Step 3: Make the summary schema strict and include confirmed facts**

Use:

~~~python
class CallSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # retain the existing fields


class CallSummaryContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_facts: dict[str, Any] = Field(default_factory=dict)
~~~

Append a JSON serialization of `ctx.confirmed_facts` under an explicit “Подтверждённые данные кандидата” heading in `PhoneSummaryProvider._body`. Record `latency_ms` with `time.perf_counter()` around the HTTP request.

- [ ] **Step 4: Write and pass the evidence exception-boundary test**

~~~python
@pytest.mark.asyncio
async def test_unexpected_evidence_error_never_aborts_call(orchestrator_env) -> None:
    orchestrator_env.evidence.maybe_capture.side_effect = RuntimeError("boom")
    result = await orchestrator_env.run_happy_call("Собеседование завтра в десять")
    assert result == "greeting_completed"
    assert orchestrator_env.closed_call.outcome is CommunicationOutcome.COMPLETED
~~~

Wrap the orchestrator call to `maybe_capture` in `except Exception`, log only exception type and session ID, and continue.

- [ ] **Step 5: Write failing API filter/field tests**

~~~python
response = await api.get(
    "/api/v1/phone/sessions",
    params={"summary_state": "done", "needs_review": "true", "outcome": "completed"},
    headers=api_headers,
)
assert response.status_code == 200
row = response.json()["sessions"][0]
assert row["summary_state"] == "done"
assert row["summary"]["summary_text"]
assert row["script_stage"] == "greeting_completed"
assert (
    "audio_evidence_url"
    in (await api.get(f"/api/v1/phone/sessions/{row['id']}", headers=api_headers)).json()["turns"][
        0
    ]
)
~~~

Run: `uv run pytest tests/integration/test_phone_api.py -q`
Expected: FAIL on missing filters or fields.

- [ ] **Step 6: Implement the REST fields and enum-validated filters**

Filter only `CommunicationChannel.CALL`. Parse enum query parameters through FastAPI types, keep `limit` bounded to 1–200, and return 422 for invalid values. Detail evidence URLs must use the authenticated admin evidence route and must not expose filesystem paths.

- [ ] **Step 7: Add the FakePhoneGate evidence-to-finalize integration test**

Drive an auto-answered call, inject a critical Russian turn, assert WAV creation, close the call, mock llmRouter once, run `finalize_pending_calls()`, and assert linked evidence plus `summary_state=done`.

Run:

~~~bash
uv run pytest tests/integration/test_phone_evidence_e2e.py tests/integration/test_phone_api.py -q
uv run ruff check app/phone app/api/phone_routes.py app/admin/phone_routes.py tests/unit/test_phone_admin_calls.py tests/integration/test_phone_api.py
uv run ruff format --check app/phone app/api/phone_routes.py app/admin/phone_routes.py tests/unit/test_phone_admin_calls.py tests/integration/test_phone_api.py
uv run mypy app fixture_site
~~~

Expected: all pass.

- [ ] **Step 8: Commit only baseline files**

~~~bash
git add app/admin/phone_routes.py tests/unit/test_phone_admin_calls.py app/phone/evidence.py app/phone/orchestrator.py app/phone/summary.py app/api/phone_routes.py tests/unit/test_phone_evidence.py tests/unit/test_phone_summary.py tests/integration/test_phone_api.py tests/integration/test_phone_evidence_e2e.py
git commit -m "fix: stabilize phone phase 2b baseline"
~~~

- [ ] **Step 9: Terra review gate**

Reviewer checks the commit against the original Phase 2b Tasks 15–16 and the four known defects: confirmed facts in prompt, strict schema, evidence exception containment, and latency metadata. Resolve verified findings in a separate `fix:` commit.

---

### Task 2: Add verification and SMS persistence schema

**Files:**

- Modify: `app/models/enums.py`
- Modify: `app/models/entities.py`
- Create: `migrations/versions/<revision>_phone_phase_2b_verification_sms.py`
- Modify: `tests/unit/test_phone_entities.py`
- Modify: `tests/unit/test_phone_enums.py`
- Modify: `tests/unit/test_phone_migration.py`
- Modify: `tests/integration/test_sqlite_migrations.py`

**Interfaces:**

- `PhoneSummaryState.PROCESSING = "processing"`.
- `PhoneVerificationStatus`: `NOT_APPLICABLE`, `PENDING`, `CONFIRMED`, `HIGH_CONFIDENCE`, `NEEDS_REVIEW`.
- `CallFactConfirmationSource`: `SMS`, `MANUAL`.
- `CommunicationSession.verification_status`, `verification_revision`, `processing_started_at`, `transport_external_id`, `related_session_id`.
- `CallFact.confirmation_source` and `confirmed_at`.
- Unique constraints `(transport, channel, transport_external_id)` and `(session_id, field)`.

- [ ] **Step 1: Write failing entity/default tests**

~~~python
async def test_call_verification_defaults(async_session, profile) -> None:
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
    assert call.verification_status is PhoneVerificationStatus.NOT_APPLICABLE
    assert call.verification_revision == 0


async def test_sms_session_accepts_string_transport_id(async_session, profile) -> None:
    sms = CommunicationSession(
        profile_id=profile.id,
        channel=CommunicationChannel.SMS,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        remote_address="+37360000000",
        remote_raw="+37360000000",
        phonegate_event_id_start=None,
        transport_external_id="incoming:1720000000000:+37360000000",
        started_at=utcnow(),
        ended_at=utcnow(),
    )
    async_session.add(sms)
    await async_session.flush()
    assert sms.phonegate_event_id_start is None
~~~

Run: `uv run pytest tests/unit/test_phone_entities.py tests/unit/test_phone_enums.py -q`
Expected: FAIL on missing enum members and columns.

- [ ] **Step 2: Add enums and ORM fields**

Use non-native enums through the existing `enum_column` helper. Add a self-FK:

~~~python
related_session_id: Mapped[UUID | None] = mapped_column(
    ForeignKey("communication_sessions.id", ondelete="SET NULL"), index=True
)
transport_external_id: Mapped[str | None] = mapped_column(String(96))
verification_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
~~~

Add the two table constraints and an index on `verification_status`. Add nullable `confirmation_source` and `confirmed_at` to `CallFact`.

- [ ] **Step 3: Generate and normalize the migration**

Create one revision after `e171bb9f241e`. The upgrade must:

1. add nullable/backfilled fields with server defaults where needed;
2. recreate the `phonesummarystate` check constraint with `processing`;
3. make `phonegate_event_id_start` nullable;
4. add both unique constraints and indexes;
5. drop temporary server defaults.

The downgrade reverses these changes and maps any `processing` row to `pending` before restoring the old enum constraint.

- [ ] **Step 4: Extend migration tests**

Assert the new head, columns, nullability, indexes, and unique constraints after `upgrade head`. Add a SQLite round-trip test that inserts both a call and an SMS session.

Run:

~~~bash
uv run pytest tests/unit/test_phone_entities.py tests/unit/test_phone_enums.py tests/unit/test_phone_migration.py tests/integration/test_sqlite_migrations.py -q
uv run alembic upgrade head
uv run alembic check
~~~

Expected: all pass and Alembic reports no new operations.

- [ ] **Step 5: Commit and Terra review**

~~~bash
git add app/models/enums.py app/models/entities.py migrations/versions tests/unit/test_phone_entities.py tests/unit/test_phone_enums.py tests/unit/test_phone_migration.py tests/integration/test_sqlite_migrations.py
git commit -m "feat: add phone verification and sms schema"
~~~

Terra verifies migration safety on SQLite and PostgreSQL semantics, self-FK deletion behavior, uniqueness, and downgrade handling.

---

### Task 3: Implement Russian critical markers, normalization, and SMS closing

**Files:**

- Create: `app/phone/critical.py`
- Modify: `app/phone/script.py`
- Modify: `app/phone/orchestrator.py`
- Create: `tests/unit/test_phone_critical.py`
- Modify: `tests/unit/test_phone_script.py`
- Modify: `tests/unit/test_phone_orchestrator.py`

**Interfaces:**

~~~python
CriticalField = Literal[
    "interview_date",
    "interview_time",
    "timezone",
    "format",
    "address",
    "meeting_url",
    "company",
    "vacancy",
]
~~~

- `has_confirmation_critical_markers(texts: Sequence[str]) -> bool`
- `normalize_critical_value(field: CriticalField, raw: str, *, reference_at: datetime,
  timezone: str = "Europe/Chisinau") -> str | None`
- `closing_for_transcript(texts: Sequence[str]) -> str`

- [ ] **Step 1: Write the normalization matrix as failing parameterized tests**

Include these exact cases with `reference_at=datetime(2026, 9, 6, 12, tzinfo=UTC)`:

~~~python
@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        ("interview_date", "завтра", "2026-09-07"),
        ("interview_date", "послезавтра", "2026-09-08"),
        ("interview_date", "12 сентября", "2026-09-12"),
        ("interview_date", "12.09.2026", "2026-09-12"),
        ("interview_time", "в 14:30", "14:30"),
        ("interview_time", "в два часа дня", "14:00"),
        ("format", "у нас в офисе", "onsite"),
        ("format", "по видеосвязи", "remote"),
        ("address", "ул. Индепенденцей, 10", "ул. Индепенденцей, 10"),
        ("interview_time", "после обеда", None),
    ],
)
def test_normalize_critical_value(field, raw, expected):
    actual = normalize_critical_value(
        cast(CriticalField, field),
        raw,
        reference_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
    )
    assert actual == expected
~~~

Add correction tests showing that normalization alone does not decide which of two conflicting expressions wins.

- [ ] **Step 2: Implement a conservative parser without a new dependency**

Use explicit Russian month/number dictionaries, `zoneinfo.ZoneInfo`, regexes for numeric dates/times, and whitespace normalization. Return `None` for unsupported or ambiguous forms. Never infer an address, timezone, or meeting format from missing text.

- [ ] **Step 3: Write closing-selection tests**

~~~python
def test_date_marker_selects_sms_closing() -> None:
    assert closing_for_transcript(["Собеседование завтра в 10"]) == SCRIPT_CLOSING_SMS


def test_noncritical_call_keeps_existing_closing() -> None:
    assert closing_for_transcript(["Позвоните Андрею позже"]) == SCRIPT_CLOSING
~~~

Define `SCRIPT_CLOSING_SMS` exactly as approved in the spec.

- [ ] **Step 4: Wire only the deterministic selector into the orchestrator**

Accumulate employer transcript text already observed during LISTENING and call:

~~~python
closing = closing_for_transcript([entry.text for entry in observed_rx_entries])
await self._say(session_id, closing)
~~~

Do not add an LLM or database query to this path.

- [ ] **Step 5: Run, commit, review**

~~~bash
uv run pytest tests/unit/test_phone_critical.py tests/unit/test_phone_script.py tests/unit/test_phone_orchestrator.py -q
uv run ruff check app/phone/critical.py app/phone/script.py app/phone/orchestrator.py tests/unit/test_phone_critical.py
uv run mypy app/phone
git add app/phone/critical.py app/phone/script.py app/phone/orchestrator.py tests/unit/test_phone_critical.py tests/unit/test_phone_script.py tests/unit/test_phone_orchestrator.py
git commit -m "feat: request sms for critical call details"
~~~

Terra checks Russian boundary cases, conservative failure behavior, and that realtime timing remains unchanged.

---

### Task 4: Build strict independent llmRouter verification passes

**Files:**

- Create: `app/phone/verification.py`
- Modify: `app/phone/summary.py`
- Modify: `app/settings/config.py`
- Modify: `.env.example`
- Create: `tests/unit/test_phone_verification.py`
- Modify: `tests/unit/test_phone_settings.py`
- Modify: `tests/unit/test_phone_summary.py`

**Interfaces:**

~~~python
class FactCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: CriticalField
    raw_expression: str
    normalized_value: str | None
    quote: str
    turn_seq: int | None
    confidence: float = Field(ge=0, le=1)
    ambiguity: str = ""


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary_text: str
    outcome_guess: Literal["interview_proposed", "info_request", "not_relevant", "unclear", "other"]
    facts: list[FactCandidate]
    review_reasons: list[str]


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[FactCandidate]
    review_reasons: list[str]


class ArbitrationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: CriticalField
    accepted_value: str | None
    supporting_quote: str
    accepted: bool
    reason: str


class ArbitrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[ArbitrationItem]


@dataclass(frozen=True)
class ModelCallMeta:
    provider: str
    model: str
    latency_ms: int
    attempts: int
~~~

`PostCallVerificationProvider` exposes these exact methods:

- `extract(ctx: VerificationContext) -> tuple[ExtractionResult, ModelCallMeta]`
- `verify(ctx: VerificationContext) -> tuple[VerificationResult, ModelCallMeta]`
- `arbitrate(ctx: VerificationContext, extracted: ExtractionResult,
  verified: VerificationResult) -> tuple[ArbitrationResult, ModelCallMeta]`
- `compare_sms(ctx: VerificationContext, sms_text: str,
  facts: Sequence[PersistedFact]) -> tuple[SmsComparisonResult, ModelCallMeta]`

- [ ] **Step 1: Write failing schema and request-isolation tests**

Verify `additionalProperties is false` recursively for all response schemas. Capture request bodies and assert the Verifier body contains neither Extractor output nor its normalized values.

~~~python
assert extractor_body != verifier_body
assert "extractor_result" not in json.dumps(verifier_body)
assert provider_schema["additionalProperties"] is False
~~~

- [ ] **Step 2: Write failure and metadata tests**

Cover transport error, HTTP 429, HTTP 500, malformed envelope, fenced JSON, unknown keys, empty content, and timeout. Assert latency is positive and sanitized errors contain no prompt, phone number, SMS, or token.

- [ ] **Step 3: Implement one private strict JSON transport**

`_complete_json(pass_name, system, user, schema_model)` posts to `/v1/chat/completions` with temperature zero, `response_format=json_schema`, the existing prefer header, and no redirects. It records `perf_counter()` latency. Public methods build separate Russian prompts and return typed results plus metadata.

- [ ] **Step 4: Add typed settings**

Add pipeline version, per-pass model overrides falling back to `effective_summary_model`, timeout, max attempts, ASR floor, processing lease seconds, and batch size. Validate enabled production configuration has an API key and all effective model names.

- [ ] **Step 5: Keep backward-compatible summary rendering**

The Extractor supplies `summary_text` and `outcome_guess` so no fourth model call is introduced. Remove the old provider implementation only after its callers and tests use `PostCallVerificationProvider`.

- [ ] **Step 6: Run, commit, review**

~~~bash
uv run pytest tests/unit/test_phone_verification.py tests/unit/test_phone_summary.py tests/unit/test_phone_settings.py -q
uv run ruff check app/phone/verification.py app/phone/summary.py app/settings/config.py tests/unit/test_phone_verification.py
uv run ruff format --check app/phone/verification.py app/phone/summary.py app/settings/config.py tests/unit/test_phone_verification.py
uv run mypy app fixture_site
git add app/phone/verification.py app/phone/summary.py app/settings/config.py .env.example tests/unit/test_phone_verification.py tests/unit/test_phone_summary.py tests/unit/test_phone_settings.py
git commit -m "feat: add independent phone verification passes"
~~~

Terra reviews prompt isolation, strict schemas, retry classification, privacy, and the two-or-three-call limit.

---

### Task 5: Reconcile evidence and persist current CallFact values

**Files:**

- Create: `app/phone/reconciliation.py`
- Create: `app/phone/facts.py`
- Create: `tests/unit/test_phone_reconciliation.py`
- Create: `tests/unit/test_phone_facts.py`

**Interfaces:**

~~~python
@dataclass(frozen=True)
class ReconciledFact:
    field: CriticalField
    raw_expression: str
    normalized_value: str | None
    source_turn_id: UUID | None
    asr_confidence: float | None
    llm_confidence: float | None
    state: CallFactState
    reason: str


@dataclass(frozen=True)
class VerificationDecision:
    status: PhoneVerificationStatus
    facts: tuple[ReconciledFact, ...]
    reasons: tuple[str, ...]
~~~

- `reconcile_verification(*, context: VerificationContext, extracted: ExtractionResult,
  verified: VerificationResult, arbitration: ArbitrationResult, asr_floor: float,
  evidence_turn_ids: Collection[UUID]) -> VerificationDecision`
- `replace_current_facts(db: AsyncSession, *, call: CommunicationSession,
  decision: VerificationDecision, pass_metadata: Mapping[str, ModelCallMeta]) -> None`

- [ ] **Step 1: Write the decision-table tests**

Cover:

- exact independent agreement plus quote plus evidence → `candidate/high_confidence`;
- quote not found → `unknown/needs_review`;
- differing normalized dates → `conflict/needs_review`;
- Arbiter rejection → `conflict/needs_review`;
- explicit ASR below floor → `unknown/needs_review`;
- absent ASR confidence with complete quote/evidence → eligible;
- missing evidence → `unknown/needs_review`;
- last unambiguous correction accepted while prior expression remains in diagnostics;
- address disagreement differing only in whitespace/case is equal;
- “после обеда” remains unknown.

- [ ] **Step 2: Implement quote and turn resolution first**

Require exact substring presence after Unicode/whitespace normalization. Resolve `turn_seq` to a persisted employer turn. A model-provided quote cannot create a source turn when the quote is absent.

- [ ] **Step 3: Implement field-by-field reconciliation**

Run `normalize_critical_value` against each pass’s raw expression. The model’s `normalized_value` is advisory and must match deterministic output for date, time, format, and address equality rules. Build a review reason for every failed gate.

- [ ] **Step 4: Write persistence idempotency tests**

Call `replace_current_facts` twice for the same revision and assert one row per `(session_id, field)`. Then change an input and assert the row updates, `verification_revision` increments only once for the new input revision, and previous candidates remain in `summary["verification"]["history"]`.

- [ ] **Step 5: Implement fact upsert and verification document**

Update existing rows in place; never delete a manual or SMS confirmation merely because a model retry ran without new source input. Persist pass metadata and candidates under `summary["verification"]` without raw secrets.

- [ ] **Step 6: Run, commit, review**

~~~bash
uv run pytest tests/unit/test_phone_reconciliation.py tests/unit/test_phone_facts.py -q
uv run ruff check app/phone/reconciliation.py app/phone/facts.py tests/unit/test_phone_reconciliation.py tests/unit/test_phone_facts.py
uv run mypy app/phone
git add app/phone/reconciliation.py app/phone/facts.py tests/unit/test_phone_reconciliation.py tests/unit/test_phone_facts.py
git commit -m "feat: reconcile and persist verified call facts"
~~~

Terra validates every trust-state transition and specifically attempts false-positive date/time cases.

---

### Task 6: Make post-call processing atomic, retryable, and versioned

**Files:**

- Modify: `app/phone/summary.py`
- Modify: `app/phone/sessions.py`
- Modify: `app/scheduler/tasks.py`
- Modify: `tests/unit/test_phone_finalize.py`
- Modify: `tests/unit/test_phone_sessions.py`
- Modify: `tests/unit/test_scheduler_phone_tasks.py`

**Interfaces:**

- `claim_pending_calls(db: AsyncSession, *, batch: int, lease_seconds: int) -> list[UUID]`
- `finalize_call(call_id: UUID) -> Literal["done", "failed", "skipped"]`
- `finalize_pending_calls() -> dict[str, int]`

- [ ] **Step 1: Write worker-claim tests**

Prove two concurrent claimers cannot receive the same call; a stale `processing_started_at` lease returns to pending; a fresh processing row is untouched; non-auto-answered and SMS sessions are never claimed.

- [ ] **Step 2: Implement atomic claims**

Use a short transaction with `SELECT FOR UPDATE SKIP LOCKED` on PostgreSQL and a compare-and-update fallback compatible with SQLite tests. Set `summary_state=processing`, `verification_status=pending`, and `processing_started_at=utcnow()` before releasing the claim transaction.

- [ ] **Step 3: Write full pipeline tests**

Mock the provider’s independent methods. Assert the order Extractor → Verifier → Arbiter, fact persistence, summary rendering, model metadata, evidence linking, and final technical/verification states.

- [ ] **Step 4: Implement `finalize_call`**

Build one immutable context, execute passes sequentially, reconcile, persist, and commit. A technical failure increments sanitized attempt metadata. Before max attempts it returns to `pending`; after exhaustion it sets `summary_state=failed` and `verification_status=needs_review`.

- [ ] **Step 5: Prevent stale retries from overwriting human/SMS confirmation**

Before commit, compare the claimed input revision and current confirmation sources. If transcript/SMS/manual state changed, discard the stale model decision and enqueue a fresh revision.

- [ ] **Step 6: Run, commit, review**

~~~bash
uv run pytest tests/unit/test_phone_finalize.py tests/unit/test_phone_sessions.py tests/unit/test_scheduler_phone_tasks.py -q
uv run ruff check app/phone/summary.py app/phone/sessions.py app/scheduler
uv run mypy app fixture_site
git add app/phone/summary.py app/phone/sessions.py app/scheduler/tasks.py tests/unit/test_phone_finalize.py tests/unit/test_phone_sessions.py tests/unit/test_scheduler_phone_tasks.py
git commit -m "feat: make phone verification pipeline atomic"
~~~

Terra reviews crash recovery, concurrent workers, retry ceilings, and stale-write protection.

---

### Task 7: Ingest and correlate PhoneGate SMS messages

**Files:**

- Modify: `app/phone/schemas.py`
- Modify: `app/phone/client.py`
- Create: `app/phone/sms.py`
- Modify: `tests/fixtures/fake_phonegate.py`
- Modify: `app/settings/config.py`
- Modify: `.env.example`
- Modify: `app/scheduler/tasks.py`
- Modify: `app/scheduler/celery_app.py`
- Modify: `tests/unit/test_phone_client.py`
- Create: `tests/unit/test_phone_sms.py`
- Modify: `tests/unit/test_scheduler_phone_tasks.py`

**Interfaces:**

~~~python
class PhoneSmsMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=96)
    address: str
    text: str = Field(min_length=1, max_length=2000)
    timestamp: int = Field(ge=0)
    direction: Literal["incoming", "outgoing"]
    status: Literal["received", "sending", "sent", "failed", "unknown"]


class PhoneSmsPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[PhoneSmsMessage]
    count: int
    synced_at: int | None = None
    syncing: bool = False
~~~

- `PhoneGateClient.sms_history(*, limit: int = 200,
  number: str | None = None) -> PhoneSmsPage`
- `PhoneGateClient.sync_sms() -> None`
- `ingest_phonegate_sms() -> dict[str, int]`

- [ ] **Step 1: Write client contract tests**

Assert Bearer auth, `GET /api/sms` query parameters, strict top-level parsing, tolerance of PhoneGate contact enrichment keys, `POST /api/sms/sync` behavior, and existing error classification.

- [ ] **Step 2: Implement read/sync client methods**

Reuse `_get` and `_post` without adding `send_sms`. Clamp limit to PhoneGate’s supported maximum.

- [ ] **Step 3: Write ingestion and correlation tests**

Cover duplicate message IDs, outgoing messages ignored for confirmation, number normalization, timestamps, exactly one call in the window, no calls, two calls, five-minute pre-end skew, 24-hour boundary, and a different profile/number.

- [ ] **Step 4: Implement idempotent SMS persistence**

For each inbound message, insert one `CommunicationSession(channel=SMS)` and one employer turn with `seq=1` and `phonegate_transcript_id=None`. Set start/end to the PhoneGate timestamp. Catch the unique-constraint race by reloading the existing row.

- [ ] **Step 5: Implement conservative correlation**

Query completed call sessions with the same normalized number and permitted time window. Set `related_session_id` only for exactly one match. For ambiguity, keep it null and record a safe reason without raw SMS text.

- [ ] **Step 6: Wire polling**

Add a phone-queue Celery task every 60 seconds. On worker startup/recovery or stale `synced_at`, call `sync_sms` once, then poll history. Settings control batch, interval, pre-skew, and 24-hour post-window.

- [ ] **Step 7: Run, commit, review**

~~~bash
uv run pytest tests/unit/test_phone_client.py tests/unit/test_phone_sms.py tests/unit/test_scheduler_phone_tasks.py -q
uv run ruff check app/phone/client.py app/phone/schemas.py app/phone/sms.py app/scheduler
uv run mypy app fixture_site
docker compose config --quiet
git add app/phone/client.py app/phone/schemas.py app/phone/sms.py app/settings/config.py app/scheduler/tasks.py app/scheduler/celery_app.py tests/fixtures/fake_phonegate.py tests/unit/test_phone_client.py tests/unit/test_phone_sms.py tests/unit/test_scheduler_phone_tasks.py .env.example
git commit -m "feat: ingest and correlate phonegate sms"
~~~

Terra reviews idempotency, timestamp units, number matching, ambiguous correlation, and confirms no outbound SMS method exists.

---

### Task 8: Verify SMS semantics and reprocess linked calls

**Files:**

- Modify: `app/phone/verification.py`
- Modify: `app/phone/sms.py`
- Modify: `app/phone/facts.py`
- Modify: `tests/unit/test_phone_verification.py`
- Modify: `tests/unit/test_phone_sms.py`
- Modify: `tests/unit/test_phone_facts.py`

**Interfaces:**

~~~python
class SmsFieldComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: CriticalField
    relation: Literal["matches", "conflicts", "not_mentioned", "ambiguous"]
    sms_expression: str
    call_expression: str
    reason: str


class SmsComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comparisons: list[SmsFieldComparison]
~~~

- `apply_sms_confirmation(db: AsyncSession, *, call: CommunicationSession,
  sms_session: CommunicationSession, comparison: SmsComparisonResult,
  metadata: ModelCallMeta) -> PhoneVerificationStatus`

- [ ] **Step 1: Write state-transition tests**

Prove:

- a matching date/time/address SMS confirms only mentioned fields;
- a partial SMS leaves other candidates unchanged;
- any conflict sets the fact to `conflict` and session to `needs_review`;
- ambiguous comparison never confirms;
- a later matching SMS cannot silently erase an earlier conflicting SMS;
- repeated processing of the same SMS is idempotent;
- SMS confirmation records `confirmed_by_turn_id`, `confirmation_source=sms`, and `confirmed_at`.

- [ ] **Step 2: Implement deterministic pre-checks**

Normalize dates, times, formats, and conservative address strings from both sides. If deterministic values disagree, record conflict without allowing the LLM to override it. If SMS does not mention a field, leave it unchanged.

- [ ] **Step 3: Call the strict SMS comparator only for plausible matches**

Pass only the relevant call facts and one SMS text. Persist metadata under the call’s verification document. Require semantic `matches` plus deterministic equality before confirmation.

- [ ] **Step 4: Requeue linked calls safely**

A new linked SMS increments the input revision and schedules comparison. It does not rerun Extractor/Verifier when transcript input is unchanged. Manual unlink reverses only conclusions sourced solely from that SMS and sends the call back through reconciliation.

- [ ] **Step 5: Run, commit, review**

~~~bash
uv run pytest tests/unit/test_phone_verification.py tests/unit/test_phone_sms.py tests/unit/test_phone_facts.py -q
uv run ruff check app/phone/verification.py app/phone/sms.py app/phone/facts.py
uv run mypy app/phone
git add app/phone/verification.py app/phone/sms.py app/phone/facts.py tests/unit/test_phone_verification.py tests/unit/test_phone_sms.py tests/unit/test_phone_facts.py
git commit -m "feat: confirm call facts from employer sms"
~~~

Terra attempts contradiction, correction, partial-message, duplicate, and wrong-call attacks before approval.

---

### Task 9: Add reliable status-aware Telegram delivery

**Files:**

- Modify: `app/phone/telegram.py`
- Modify: `app/phone/summary.py`
- Modify: `app/scheduler/tasks.py`
- Modify: `app/scheduler/celery_app.py`
- Modify: `app/settings/config.py`
- Modify: `.env.example`
- Modify: `tests/unit/test_phone_telegram.py`
- Modify: `tests/unit/test_phone_finalize.py`
- Modify: `tests/unit/test_scheduler_phone_tasks.py`

**Interfaces:**

~~~python
@dataclass(frozen=True)
class TelegramDeliveryResult:
    message_id: int
~~~

- `send_telegram_message(*, token: str, chat_id: str, text: str,
  client: httpx.AsyncClient | None = None, timeout: float = 10.0)
  -> TelegramDeliveryResult`
- `render_call_notification(*, call: CommunicationSession, facts: Sequence[CallFact],
  company: str | None, vacancy: str | None, base_url: str | None) -> str`
- `deliver_pending_phone_notifications() -> dict[str, int]`

- [ ] **Step 1: Write rendering tests for all trust states**

Assert Russian labels, safe facts, alternatives for conflicts, short quotes, admin link, HTML escaping, and absence of raw phone number. A `needs_review` card must never phrase a disputed value as confirmed.

- [ ] **Step 2: Write retry classification tests**

HTTP 429 and definite 5xx responses become `retrying` with bounded exponential backoff. HTTP 400/401/403 become terminal `failed`. A transport timeout after request submission is marked `failed` with `ambiguous_delivery=true` and is not blindly retried.

- [ ] **Step 3: Implement delivery claims and idempotency metadata**

Persist `input_revision`, `state`, `attempts`, `next_attempt_at`, and returned Telegram `message_id` inside `summary["telegram"]`. Send only when the stored revision matches the current verification revision.

- [ ] **Step 4: Separate notification delivery from fact processing**

`finalize_call` creates or refreshes `telegram.state=pending`. A dedicated phone-queue task delivers due messages, so Telegram latency/failure never holds a fact-processing transaction.

- [ ] **Step 5: Run, commit, review**

~~~bash
uv run pytest tests/unit/test_phone_telegram.py tests/unit/test_phone_finalize.py tests/unit/test_scheduler_phone_tasks.py -q
uv run ruff check app/phone/telegram.py app/phone/summary.py app/scheduler
uv run mypy app fixture_site
git add app/phone/telegram.py app/phone/summary.py app/scheduler/tasks.py app/scheduler/celery_app.py app/settings/config.py .env.example tests/unit/test_phone_telegram.py tests/unit/test_phone_finalize.py tests/unit/test_scheduler_phone_tasks.py
git commit -m "feat: retry phone telegram notifications safely"
~~~

Terra checks privacy, HTML injection, ambiguous delivery, revision races, and independence from summary/fact state.

---

### Task 10: Build the verification queue, review controls, and complete REST API

**Files:**

- Modify: `app/admin/phone_routes.py`
- Modify: `app/admin/templates/_calls_history.html`
- Modify: `app/admin/templates/_calls_history_detail.html`
- Modify: `app/api/phone_routes.py`
- Modify: `tests/unit/test_phone_admin_calls.py`
- Create: `tests/integration/test_phone_admin_review.py`
- Modify: `tests/integration/test_phone_api.py`

**Interfaces:**

- Admin POST `/admin/phone/calls/{call_id}/facts/{field}/review` with action `confirm`, `correct`, or `unknown`.
- Admin POST `/admin/phone/calls/{call_id}/sms/{sms_id}/link`.
- Admin POST `/admin/phone/calls/{call_id}/sms/{sms_id}/unlink`.
- API list filters `verification_status`, `summary_state`, `telegram_state`, `needs_review`, and `outcome`.
- Detail returns current facts, pass decisions, review reasons, related/unlinked SMS metadata, audit entries, and evidence URLs.

- [ ] **Step 1: Write authenticated context/API tests**

Seed calls in every trust state and assert filtering, pagination, masked numbers, safe model metadata, SMS relations, facts, and evidence URLs. List endpoints must never return raw SMS text; authenticated detail may return the related SMS text.

- [ ] **Step 2: Write CSRF and validation tests for every mutation**

Assert unauthenticated 401/redirect behavior, invalid CSRF 403, cross-call fact/SMS IDs 404, invalid action 404, invalid normalized date/time 422, and successful 303 redirects.

- [ ] **Step 3: Implement server-side review service calls**

For manual confirmation/correction, update `CallFact`, set `confirmation_source=manual` and `confirmed_at`, recompute session status, increment revision, and record `phone.fact.confirmed`, `phone.fact.corrected`, or `phone.fact.unknown` with old/new normalized values. Do not put raw SMS or transcript text in audit details.

- [ ] **Step 4: Implement link/unlink mutations**

Require matching normalized phone numbers. A manual link supersedes ambiguity but records an audit event. Unlink only that relation, invalidates SMS-sourced confirmations from that SMS, and requeues comparison.

- [ ] **Step 5: Render the operational UI**

History badges use `Подтверждено по SMS/вручную`, `Высокая уверенность`, and `Нужна проверка`. Detail shows one fact row with value, source quote, evidence player, three model decisions, SMS comparison, reason, and review form. Show Telegram state and model latency without credentials or full numbers.

- [ ] **Step 6: Run focused tests and three local browser repetitions**

~~~bash
uv run pytest tests/unit/test_phone_admin_calls.py tests/integration/test_phone_admin_review.py tests/integration/test_phone_api.py -q
for run in 1 2 3; do
  RUN_PLAYWRIGHT_TESTS=1 uv run pytest tests/integration/test_phone_admin_review.py -q
done
uv run ruff check app/admin/phone_routes.py app/api/phone_routes.py tests/unit/test_phone_admin_calls.py tests/integration/test_phone_admin_review.py
uv run mypy app fixture_site
~~~

Expected: three consecutive green runs.

- [ ] **Step 7: Commit and Terra review**

~~~bash
git add app/admin/phone_routes.py app/admin/templates/_calls_history.html app/admin/templates/_calls_history_detail.html app/api/phone_routes.py tests/unit/test_phone_admin_calls.py tests/integration/test_phone_admin_review.py tests/integration/test_phone_api.py
git commit -m "feat: add phone fact review workflow"
~~~

Terra performs both code review and visual/admin workflow review, including narrow viewport, empty, loading, failed, ambiguous SMS, conflict, and missing evidence states.

---

### Task 11: Complete mocked E2E coverage and operational documentation

**Files:**

- Create: `tests/integration/test_phone_verification_e2e.py`
- Modify: `tests/integration/test_phone_evidence_e2e.py`
- Modify: `tests/fixtures/fake_phonegate.py`
- Modify: `docs/security.md`
- Modify: `docs/superpowers/specs/2026-09-05-phone-call-agent-phase-2b-design.md`
- Modify: `README.md`

**Interfaces:**

- One deterministic E2E harness drives call → evidence → three LLM responses → facts → Telegram state → SMS import → confirmation.
- A second scenario drives an SMS conflict → `needs_review`.
- A failure scenario proves provider/PhoneGate/Telegram degradation remains recoverable.

- [ ] **Step 1: Add the high-confidence E2E scenario**

Use `FakePhoneGate` and `httpx.MockTransport`. Inject “Собеседование 12 сентября в 14:30, улица Индепенденцей 10”, return agreeing strict pass payloads, and assert candidate facts, evidence, `high_confidence`, and a pending/sent fake Telegram card.

- [ ] **Step 2: Add matching and conflicting SMS scenarios**

Import a matching message and assert `confirmed` plus idempotent re-import. Import a conflicting “13 сентября” message in an isolated call and assert `conflict/needs_review` with no confirmed date.

- [ ] **Step 3: Add crash/recovery scenarios**

Cover stale worker lease, one llmRouter transient response followed by success, SMS sync failure followed by history recovery, Telegram 429 followed by success, and evidence exception without call abort.

- [ ] **Step 4: Update documentation**

Document settings, states, privacy, evidence retention, review procedure, safe retry behavior, DEV startup, live-test flags, and the explicit absence of calendar/outbound SMS/realtime LLM behavior. Mark the old Phase 2b scope as superseded by the production-readiness spec.

- [ ] **Step 5: Run the full local sweep**

~~~bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app fixture_site
uv run pytest
docker compose config --quiet
~~~

Expected: all pass. Do not weaken tests, warnings, or lint configuration to obtain green output.

- [ ] **Step 6: Commit and Terra review**

~~~bash
git add tests/integration/test_phone_verification_e2e.py tests/integration/test_phone_evidence_e2e.py tests/fixtures/fake_phonegate.py docs/security.md docs/superpowers/specs/2026-09-05-phone-call-agent-phase-2b-design.md README.md
git commit -m "test: cover phone verification end to end"
~~~

Terra checks the complete spec-to-test matrix and verifies failures are meaningful rather than implementation mirrors.

---

### Task 12: Run real DEV acceptance and final branch review

**Files:**

- Create: `tests/realcall/test_realcall_phase_2b.py`
- Modify: `tests/realcall/README.md`
- Modify: `tests/realcall/a06_originate.py` only if a tested SMS helper is required
- Create: `docs/operations/phone-phase-2b-dev-acceptance.md`

**Interfaces:**

- Opt-in real llmRouter test cases.
- Opt-in real Telegram delivery check.
- A06 automated GSM call with Russian critical details.
- Real PhoneGate SMS matching and conflict checks.
- Recorded acceptance evidence containing timestamps, sanitized IDs, commands, and results.

- [ ] **Step 1: Add opt-in real-integration tests**

The module skips unless `ENABLE_REALCALL_TESTS=true` and required credentials/endpoints exist. Add tests that:

1. run Extractor, Verifier, and Arbiter against real llmRouter for absolute date, relative date, correction, ambiguity, and contradiction fixtures;
2. send one real DEV Telegram card and assert a returned message ID;
3. call PhoneGate status, SMS sync/history, and evidence endpoints without logging secrets.

- [ ] **Step 2: Extend the A06 happy path**

A06 calls the DEV PhoneGate line and injects:

> Звоню по вакансии кладовщика. Собеседование двенадцатого сентября в четырнадцать тридцать, по адресу улица Индепенденцей десять.

Assert the downlink contains the SMS-request closing, the employer turn is persisted, evidence exists, the Celery result reaches `high_confidence`, and Telegram delivery is `sent`.

- [ ] **Step 3: Exercise real SMS confirmation and conflict**

Use the approved test rig to send a matching SMS, poll until JobHunter imports it, and assert `confirmed`. In a separate isolated call/revision, send a conflicting date and assert `needs_review`. Record only masked numbers and sanitized session/message IDs.

- [ ] **Step 4: Start and validate the complete DEV stack**

Run the repository’s DEV Compose project with PostgreSQL, Redis, web, Celery phone queue, and phone-agent. Then run:

~~~bash
RUN_SERVICE_INTEGRATION_TESTS=1 DATABASE_URL="$DEV_DATABASE_URL" uv run alembic upgrade head
RUN_SERVICE_INTEGRATION_TESTS=1 DATABASE_URL="$DEV_DATABASE_URL" uv run alembic check
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
ENABLE_REALCALL_TESTS=true uv run pytest tests/realcall/test_realcall_phase_2b.py -vv
~~~

Use the actual DEV URL/port from Compose if different. Inspect Celery/phone-agent logs for sanitized failures, restart Celery once, and prove pending work resumes without duplicates.

- [ ] **Step 5: Run admin acceptance three consecutive times**

Terra logs into the actual DEV admin and checks list filters, call detail, facts, evidence playback, SMS link/unlink, manual correction, audit entries, Telegram state, empty/error states, and responsive layout.

~~~bash
for run in 1 2 3; do
  RUN_PLAYWRIGHT_TESTS=1 DEV_BASE_URL="$DEV_BASE_URL" uv run pytest tests/integration/test_phone_admin_review.py -q
done
~~~

Expected: all three runs pass.

- [ ] **Step 6: Run final repository verification**

~~~bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app fixture_site
uv run pytest
git status --short
git diff --check
~~~

Expected: all checks pass; only intentional acceptance documentation changes remain.

- [ ] **Step 7: Record acceptance and commit**

Write actual timestamps, service versions, sanitized IDs, commands, results, and known operational limits in `docs/operations/phone-phase-2b-dev-acceptance.md`.

~~~bash
git add tests/realcall/test_realcall_phase_2b.py tests/realcall/README.md tests/realcall/a06_originate.py docs/operations/phone-phase-2b-dev-acceptance.md
git commit -m "test: validate phone phase 2b on real dev services"
~~~

Do not stage `tests/realcall/a06_originate.py` if it did not change.

- [ ] **Step 8: Whole-branch Sol review**

`gpt-5.6-sol` reviews every commit from the branch merge-base through HEAD for spec coverage, correctness, migration safety, privacy, concurrency, API/admin behavior, tests, and operational readiness. Verified findings return to Luna for fixes and Terra for re-review. Escalate an unresolved complex issue to `gpt-6-astra`.

- [ ] **Step 9: Final manual-call handoff**

After Sol findings are closed and final checks are rerun, provide the operator with the DEV number, expected Russian prompt, expected SMS closing, and the admin/Telegram observations. Ask the operator to make one real manual call. Inspect that call’s persisted turns, evidence, three pass results, facts, Telegram delivery, and admin display before declaring Phase 2b complete.

---

## Self-Review

### Spec coverage

| Specification requirement | Plan task |
|---|---|
| Existing Phase 2b defects and missing REST/E2E work | 1 |
| Verification/SMS schema and migration | 2 |
| Russian-only deterministic closing and normalization | 3 |
| Independent Extractor/Verifier/Arbiter and strict schemas | 4 |
| Quote/evidence/ASR reconciliation and `CallFact` state | 5 |
| Atomic claims, retry, revision, crash recovery | 6 |
| PhoneGate SMS read/sync, persistence, correlation | 7 |
| SMS semantic confirmation and conflict handling | 8 |
| Telegram status, privacy, bounded retry | 9 |
| Admin filters, evidence, correction, SMS link, audit | 10 |
| Mocked full-pipeline coverage and documentation | 11 |
| DEV PostgreSQL/Compose and real llmRouter/PhoneGate/Telegram/GSM | 12 |
| Three consecutive admin runs | 10 and 12 |
| Luna implementation, Terra task/admin review, Sol branch review, Astra escalation | Global constraints and every task |
| Final operator manual call | 12 |

### Placeholder scan

The plan contains no `TODO`, `TBD`, “implement later”, abbreviated function bodies, or
unspecified error-handling steps. The Alembic filename intentionally uses `<revision>`
because Alembic generates the revision identifier at execution time. DEV environment
variable values are deliberately read from the authorized environment and never copied
into the plan.

### Type consistency

`CriticalField`, `FactCandidate`, `ModelCallMeta`, `VerificationDecision`, `PhoneVerificationStatus`, `CallFactConfirmationSource`, `SmsComparisonResult`, and all public function signatures are introduced before consumers. Technical `summary_state` remains independent from `verification_status`. SMS confirmation references a `CommunicationTurn`; manual confirmation has no confirming turn. Extractor supplies the summary, keeping initial processing to two or three llmRouter calls.
