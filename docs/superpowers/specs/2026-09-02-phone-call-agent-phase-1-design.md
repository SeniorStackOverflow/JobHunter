# Phone Call Agent — Phase 1 design

Date: 2026-09-02
Status: approved design, ready for implementation planning
Parent architecture: `docs/phonegate-call-agent-architecture.md` (§34.1, §40.1)
Related: PhoneGate (`/home/andrei/projects/PhoneGate`, baseline commit `45f04ea`)

## 1. Context and scope

The parent architecture defines a six-phase program for autonomous employer-call
handling in JobHunter over PhoneGate. This spec covers **Phase 1 only**: the data
model and a read-only integration layer that observes and persists real PhoneGate
call activity, so later phases build dialogue and scheduling on a stable
foundation instead of temporary structures.

Phase 1 is a **pure observer of PhoneGate**. The call agent never calls
`answer`, `speak`, `dial`, or `send_sms` — it issues only `GET` requests. It
does persist on the JobHunter side: call sessions, transcript turns, and (on a
first match) a `PHONE` `EmployerContact` row for correlation. Employer calls in
Phase 1 are answered by a human or by PhoneGate's own behavior; JobHunter only
watches and records.

### 1.1 In scope

- `ContactType.PHONE` enum value.
- Four new tables: `communication_sessions`, `communication_turns`,
  `call_facts`, `interview_appointments`. Phase 1 writes only the first two;
  the other two are created for schema stability.
- `phone_channel_health` table.
- `app/phone/` package: REST client, ingestion loop, correlation, health.
- A dedicated long-lived asyncio process `job-agent phone-agent`, deployed as a
  new Compose service `call-agent`.
- `GET /api/v1/phone/status`, `GET /api/v1/phone/sessions`,
  `GET /api/v1/phone/sessions/{id}`.
- A "Phone channel" section inside the existing admin **Диагностика** view.
- Settings block for PhoneGate connectivity and poll cadence.
- Fake PhoneGate test fixture; unit, integration, migration, and opt-in live
  tests.

### 1.2 Out of scope (later phases)

- Any autonomous or operator-triggered speech, auto-answer policy, operator
  takeover controls (Phase 2).
- Evidence audio clips (Phase 2).
- Realtime LLM dialogue, ASR trust classing, `CallFact` extraction, targeted
  repair (Phase 3).
- `AvailabilityService`, calendar, `InterviewAppointment` writes, critical-fact
  read-back (Phase 4).
- A06 GSM canary, latency histograms, provider-failover telemetry (Phase 5).
- The top-level `Звонки` admin section and its tabs (§39; later phase).
- Unix-domain-socket / Docker-bridge transport to PhoneGate — Phase 1 uses the
  public HTTPS endpoint via a config setting; the elegant same-host path is a
  separate hardening task.
- Outbound calls/SMS.

## 2. Goals and non-goals

### Goals

1. Persist every inbound PhoneGate call as a `CommunicationSession` with correct
   lifecycle timestamps and outcome.
2. Persist every transcript line as an idempotent `CommunicationTurn`.
3. Correlate a caller number to an `Application` / `profile_id` when the number
   is known, and fall back to the default profile otherwise.
4. Expose phone-channel health as an independent domain that never affects
   global `/ready`.
5. Survive PhoneGate restarts and agent restarts without duplicate or corrupt
   session data.
6. Ship a fake PhoneGate so the whole loop is testable with no live device.

### Non-goals

- Sub-second latency. Phase 1 polls at ~0.3–1 s; latency matters only from
  Phase 3.
- Completeness of health components. Phase 1 reports what it can observe;
  `asr` / `tts` / `llm_realtime` / `sms` are `unknown`.
- Multi-line concurrency. One A14 = one line = one active session.

## 3. Architecture overview

```text
PhoneGate Web Studio (VPS, 127.0.0.1:8888, public via Caddy)
        │  REST  /api/device/status  /api/events  /api/call/transcript
        ▼
jobhunter-call-agent            ← new long-lived asyncio process (Compose: call-agent)
  ├── PhoneGateClient           persistent httpx.AsyncClient, bearer auth
  ├── ingest loop               single task: poll → dispatch → DB writes
  │     ├── event cursor        Redis  phone:events:cursor
  │     ├── session lifecycle   communication_sessions
  │     ├── turn ingestion      communication_turns
  │     └── correlation         E.164 → EmployerContact(PHONE) → Application → profile
  ├── health snapshot           phone_channel_health  (upsert per component)
  └── Redis singleton lock      leased_redis_lock("phone-agent-singleton")
        │
        ▼
PostgreSQL  ← same app.database.async_session_factory as Celery
        ▲
        │  read-only
FastAPI api  GET /api/v1/phone/status | /sessions | /sessions/{id}
Admin        Диагностика → "Phone channel" section
```

The agent shares the `app` package, `Settings`, and DB session factory with the
API and Celery worker. It adds no dependency on Celery and is not a Celery task.

## 4. `app/phone/` module layout

```text
app/phone/
  __init__.py
  numbers.py            E.164 normalization (extracted shared helper; see §8.1)
  client.py             PhoneGateClient
  schemas.py            pydantic models for PhoneGate payloads + internal DTOs
  states.py             TelephonyState enum
  correlation.py        CallerCorrelation service
  health.py             health snapshot computation + persistence
  ingest.py             IngestLoop: cursor, event dispatch, session/turn writes
  agent.py              run(): process wiring, signals, singleton lock, heartbeat
```

### 4.1 `client.py` — PhoneGateClient

Thin async wrapper over one `httpx.AsyncClient` (base_url = `phonegate_url`,
`Authorization: Bearer <phonegate_auth_token>`, keep-alive, timeout =
`phone_http_timeout_seconds`). Methods, read-only subset only:

```text
async health() -> dict
async device_status() -> DeviceStatus
async events(after_id: int, limit: int = 250) -> EventsPage
async transcript(after_id: int = 0, limit: int = 250) -> TranscriptPage   # reconcile only
```

No `answer` / `speak` / `dial` / `send_sms` methods exist in Phase 1 — they are
added in Phase 2 with their policy gates. The client raises a typed
`PhoneGateUnavailable` on transport/5xx errors and `PhoneGateError` on 4xx; it
never retries internally (the loop owns cadence).

### 4.2 `schemas.py`

Pydantic models mirroring the PhoneGate payloads actually consumed:

- `DeviceStatus`: `connected`, `mode`, `is_daemon_mode` (derive from `mode`),
  `call_state`, `caller_number`, `caller_name`, `rx_audio_stats`,
  `daemon_version`, `device` (battery, operator, sim_*), `latest_event_id`.
- `PhoneEvent`: `id: int`, `type: Literal["incoming_call","call_state","transcript"]`,
  `timestamp: int`, `data: dict`. Unknown types are ignored, not an error.
- `EventsPage`: `events: list[PhoneEvent]`, `latest_id: int`,
  `last_incoming_call: dict | None`.
- `TranscriptEntry`: `id`, `speaker` (`rx`/`tx`), `text`, `meta`, `backend`,
  `confidence`, `timestamp_ms`.
- Internal DTOs: `CorrelationResult`, `HealthComponent`.

Parsing is lenient: missing optional fields default; a malformed single event is
logged and skipped, the cursor still advances past it.

### 4.3 `states.py`

```python
class TelephonyState(StrEnum):
    IDLE = "idle"
    RINGING = "ringing"
    CONNECTED = "connected"
    ENDED = "ended"
```

PhoneGate exposes only `IDLE` / `RINGING` / `IN_CALL`; `IN_CALL` maps to
`CONNECTED`. The richer states from §7.1 (`ANSWERING`, `ENDING`, outbound
states) are not needed by an observer and are added when the agent gains call
control.

The ingest loop normalizes `DeviceStatus.call_state` to `TelephonyState` once
per cycle and uses that value for dispatch decisions, health derivation, and
logging. `CommunicationSession` stores no state column — session state is
derived from its timestamps and `ended_at`.

## 5. Process, deployment, settings

### 5.1 Entrypoint

Add a `phone-agent` subcommand to `app/cli.py`:

```text
job-agent phone-agent    →    asyncio.run(app.phone.agent.run())
```

`run()`:

1. Load `Settings`. If `not phone_agent_enabled`: log and exit 0 (lets the
   Compose service exist but stay dormant until enabled).
2. Acquire `leased_redis_lock("phone-agent-singleton", ttl=...)`. If not
   acquired: log and exit non-zero (Compose restarts; the holder keeps running).
3. Build `PhoneGateClient` and confirm `health()` once (log, but do not block
   startup on failure — the loop reports it as `unavailable`).
4. Start the ingest loop. Install SIGINT/SIGTERM handlers that cancel the loop,
   close the client, release the lock, and exit. In-flight open sessions are
   left as-is; the next start reconciles them (§7.5).
5. Touch a heartbeat file (`/tmp/phone-agent-alive`) after each successful poll
   cycle for the Compose healthcheck.

### 5.2 Compose service

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
    test:
      - CMD-SHELL
      - >-
        test -f /tmp/phone-agent-alive &&
        test "$(find /tmp/phone-agent-alive -mmin -2)" != ""
    interval: 30s
    timeout: 5s
    retries: 3
  restart: unless-stopped
  stop_grace_period: 15s
  logging: *default-logging
```

When `phone_agent_enabled=false` the process exits 0 immediately; the container
then sits in a restart loop that is harmless but noisy. Acceptable for Phase 1;
document that operators leave the service out of the `up` set until enabling, or
we add a `sleep`-and-recheck idle mode. Decide during planning (§14).

### 5.3 Settings (`app/settings/config.py`)

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

- `phonegate_auth_token` joins the `empty_secret_is_unset` validator list.
- `validate_secure_production`: if `phone_agent_enabled` and
  `phonegate_auth_token is None` → raise.
- `.env.example` gets commented placeholders. The real `phonegate_url`
  (`https://phonegate.46-225-103-75.sslip.io`) and token go in `.env`.
- `readiness_status()` is **not** modified.

### 5.4 Concurrency and safety

- Redis singleton lock for the process; loss of lease → log and exit.
- In-process `self._open_session_id: UUID | None` is the single-active-call
  guard.
- All DB writes use `app.database.async_session_factory` with short
  transactions (one per poll cycle's mutations, or one per event — decide in
  planning; favor one commit per cycle).

## 6. Data model

### 6.1 Enums (`app/models/enums.py`)

```python
class ContactType(StrEnum):
    EMAIL = "email"
    APPLICATION_URL = "application_url"
    INTERNAL_JOB_BOARD = "internal_job_board"
    PHONE = "phone"                     # new

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

All use `enum_column(..., native_enum=False)`. SQLAlchemy 2.0 non-native enums
default `create_constraint=False`, so adding `PHONE` needs no CHECK-constraint
DDL; the migration still adds an explicit no-op-safe step and a comment, and
`alembic check` must stay clean.

### 6.2 `communication_sessions`

| column | type | notes |
| --- | --- | --- |
| `id` | Uuid PK | |
| `profile_id` | Uuid FK `user_profiles` CASCADE, indexed | resolved at open; default-profile fallback |
| `application_id` | Uuid FK `applications` SET NULL, nullable | correlated |
| `canonical_job_id` | Uuid FK `canonical_jobs` SET NULL, nullable | |
| `source_job_id` | Uuid FK `source_jobs` SET NULL, nullable | |
| `contact_id` | Uuid FK `employer_contacts` SET NULL, nullable | matched PHONE contact |
| `channel` | enum `CommunicationChannel` | Phase 1 always `call` |
| `transport` | String(32) | `phonegate` |
| `direction` | enum `CommunicationDirection` | Phase 1 always `inbound` |
| `remote_address` | String(32) | E.164 caller, `""` if withheld |
| `remote_raw` | String(64) | raw caller string from PhoneGate |
| `phonegate_event_id_start` | Integer | event id that opened the session |
| `started_at` | DateTime(tz) | first event for this session |
| `ringing_at` | DateTime(tz) nullable | |
| `answered_at` | DateTime(tz) nullable | |
| `ended_at` | DateTime(tz) nullable | null ⇒ open |
| `outcome` | enum `CommunicationOutcome` nullable | set on close |
| `needs_review` | Boolean default false | |
| `rx_frame_stats` | JSON default dict | last `rx_audio_stats` for the call |
| `diagnostics` | JSON default dict | daemon version, operator, resync notes |
| `created_at` / `updated_at` | DateTime(tz) | `updated_at` via `onupdate` |

Indexes: `(profile_id, started_at)`, `(remote_address, started_at)`, `ended_at`.
Single-open invariant is enforced by the agent (Redis lock + `self._open_session_id`
+ a `WHERE ended_at IS NULL` guard query on start), not a DB constraint.

### 6.3 `communication_turns`

| column | type | notes |
| --- | --- | --- |
| `id` | Uuid PK | |
| `session_id` | Uuid FK `communication_sessions` CASCADE, indexed | |
| `phonegate_transcript_id` | Integer nullable | PhoneGate transcript row id |
| `seq` | Integer | monotonic within session |
| `speaker` | enum `TurnSpeaker` | `rx`→`employer`, `tx`→`operator` |
| `text` | Text | |
| `raw_text` | Text nullable | reserved; equals `text` in Phase 1 |
| `asr_backend` | String(32) nullable | transcript `backend` |
| `asr_confidence` | Float nullable | transcript `confidence` |
| `asr_meta` | String(255) nullable | transcript `meta` string |
| `occurred_at` | DateTime(tz) | from transcript `timestamp_ms` |
| `created_at` | DateTime(tz) | |

Unique constraint `(session_id, phonegate_transcript_id)` → idempotent
ingestion. `seq` assigned by the agent as `count(existing turns) + 1`.

### 6.4 `call_facts` (created, unwritten in Phase 1)

Columns per architecture §24: `id`, `session_id` FK CASCADE, `source_turn_id` FK
`communication_turns` SET NULL nullable, `field` String(64), `raw_expression`
Text, `normalized_value` String(500) nullable, `asr_confidence` Float nullable,
`llm_confidence` Float nullable, `state` enum `CallFactState`,
`confirmed_by_turn_id` FK `communication_turns` SET NULL nullable, `created_at`,
`updated_at`.

### 6.5 `interview_appointments` (created, unwritten in Phase 1)

Columns per architecture §24: `id`, `profile_id` FK CASCADE, `application_id` FK
SET NULL nullable, `communication_session_id` FK `communication_sessions` SET
NULL nullable, `starts_at` DateTime(tz) nullable, `timezone` String(64),
`format` enum `InterviewFormat`, `address` String(500) nullable, `meeting_url`
String(2048) nullable, `contact_person` String(255) nullable, `preparation` Text
nullable, `status` enum `InterviewStatus`, `confirmed_at` DateTime(tz) nullable,
`created_at`, `updated_at`.

### 6.6 `phone_channel_health`

| column | type | notes |
| --- | --- | --- |
| `component` | String(32) PK | `phonegate_transport`, `a14_daemon`, `gsm_line`, `sim_account`, `agent` |
| `status` | enum `PhoneComponentStatus` | |
| `detail` | String(500) nullable | short human string, no secrets |
| `last_ok_at` | DateTime(tz) nullable | last time this component was healthy |
| `updated_at` | DateTime(tz) | every write |

Small fixed row set, upserted every poll cycle. No history table in Phase 1.

### 6.7 Migration

One Alembic revision, `down_revision = "5191960d5cc9"`, using
`op.batch_alter_table` / `op.create_table` in the repo's existing style. Creates
the five tables and their enums, indexes, unique and foreign-key constraints.
`ContactType.PHONE` needs no column DDL but the revision documents it.
`tests/integration/test_sqlite_migrations.py` must pass; `alembic upgrade head &&
alembic check` clean.

### 6.8 Models

All five entities added to `app/models/entities.py` following existing mixin
conventions (`UUIDPrimaryKeyMixin`, `TimestampMixin` where both timestamps are
wanted, explicit `created_at` otherwise).

## 7. Ingestion loop (`ingest.py`)

### 7.1 Poll cycle

```text
loop:
  status = client.device_status()
  health.update_from_status(status)

  page = client.events(after_id=cursor, limit=250)

  if page.latest_id < cursor:                      # PhoneGate restarted
      handle_resync(status)
      cursor = page.latest_id
  else:
      for event in sorted(page.events, key=id):
          dispatch(event, status)
      cursor = max(cursor, max(e.id for e in page.events), page.latest_id)

  persist_cursor(cursor)                           # Redis phone:events:cursor
  health.mark_poll_ok()
  touch_heartbeat_file()

  sleep(active_interval if status.call_state != "IDLE" else idle_interval)
```

Every PhoneGate call is wrapped: `PhoneGateUnavailable` / `PhoneGateError` →
`health.mark_transport_error(exc)`, skip the rest of the cycle, sleep
`idle_interval`, continue. The loop never crashes on a PhoneGate error.

### 7.2 Event dispatch

- **`incoming_call`**
  - No open session → open one: `direction=inbound`, `channel=call`,
    `transport=phonegate`, `started_at`/`ringing_at = now`, `remote_raw` from
    `data.caller_number`, `remote_address = normalize_e164(remote_raw)` (or `""`),
    `phonegate_event_id_start = event.id`. Run correlation (§8), store
    `profile_id` / `application_id` / `canonical_job_id` / `source_job_id` /
    `contact_id`. Set `self._open_session_id`.
  - Open session, same caller (same normalized `remote_address`, or same
    `remote_raw` when unnormalizable) → ignore.
  - Open session, different caller → close current as `abandoned`
    (`ended_at=now`), open a new session.

- **`call_state`**
  - `RINGING` → set `ringing_at` if unset.
  - `IN_CALL` → set `answered_at` if unset.
  - `IDLE` → close the open session: `ended_at=now`,
    `outcome = COMPLETED if answered_at else MISSED`, copy final
    `status.rx_audio_stats` into `rx_frame_stats`, clear `self._open_session_id`.

- **`transcript`**
  - Resolve the target session: the open session, else — if a call is currently
    active per `status` — a synthetic session opened from `status`
    (`needs_review=true`, `diagnostics.note="transcript_before_session_start"`),
    else skip (stale transcript after call end; log).
  - Upsert `CommunicationTurn` keyed by `(session_id, phonegate_transcript_id)`.
    `speaker`: `rx`→`EMPLOYER`, `tx`→`OPERATOR`. `seq = existing_count + 1`.
  - PhoneGate writes a `tx` transcript row on `speak` *accept*, before uplink is
    confirmed. Phase 1 has no assistant speech, so any `tx` line is a human
    operator using PhoneGate's own studio — recorded as `OPERATOR`, no delivery
    reconciliation needed. (Assistant-turn delivery reconciliation is a Phase 2
    concern.)

### 7.3 Session close on `IDLE`

Always driven by a `call_state` `IDLE` event. If the loop sees
`status.call_state == "IDLE"` but still holds `self._open_session_id` and no
`IDLE` event arrived (event lost / buffer evicted), close the session on the
next cycle with `outcome=UNKNOWN`, `needs_review=true`,
`diagnostics.note="closed_from_status_no_idle_event"`.

### 7.4 Cursor

- Stored in Redis: `phone:events:cursor` (int, no TTL).
- On start: read it. If absent → `cursor = status.latest_event_id` (do not
  replay history). If present but `> status.latest_event_id` → treat as a
  restart, `cursor = status.latest_event_id`.

### 7.5 Reconcile on agent start / resync

`handle_resync(status)` and the start path share logic:

1. If an open session exists locally (from a previous process) — find it by
   `ended_at IS NULL`. If `status.call_state == "IDLE"`: close it
   `outcome=UNKNOWN`, `needs_review=true`. If a call is active: keep it open,
   set `self._open_session_id`, append a `diagnostics` resync note.
2. If `status.call_state == "RINGING"` and no open session: open one from
   `status`.
3. Continue polling from the reconciled cursor.

### 7.6 Buffer-eviction guard

`/api/events` keeps 250 entries. At the configured cadence a gap is unlikely,
but if `page.events` is non-empty and its minimum id `> cursor + 1`, log
`events_gap` with the range and set `needs_review=true` on the open session (if
any). No transcript backfill in Phase 1 beyond what `/api/call/transcript`
reconciliation could add — defer that to Phase 2.

### 7.7 Audit

On session open and on correlation resolution, write one
`AuditEvent(actor="phone-agent", action="communication_session.opened" /
".correlated", entity_type="communication_session", entity_id=<id>,
sanitized_details={masked caller, matched application_id, profile_id})`. Caller
numbers are masked in audit and logs (`+373••••123`).

## 8. Correlation (`correlation.py`)

```text
normalize_e164(remote_raw, region=phone_caller_region)  →  e164 | None

if e164 is None:
    return default-profile fallback

contact = SELECT EmployerContact
          WHERE contact_type = PHONE AND value = e164
          ORDER BY confidence DESC, created_at DESC   LIMIT 1

if contact is None:
    job = SELECT SourceJob WHERE public_phone = e164
          ORDER BY last_seen_at DESC   LIMIT 1
    if job and job.canonical_job_id:
        contact = create EmployerContact(
            canonical_job_id=job.canonical_job_id, source_job_id=job.id,
            value=e164, contact_type=PHONE,
            discovery_source="inbound_call_match_public_phone",
            official_domain=domain(job.employer_url or job.canonical_url),
            verification_status=UNVERIFIED, confidence=0.6,
            evidence_url=job.canonical_url)

if contact is None:
    return default-profile fallback

application = SELECT Application
              WHERE canonical_job_id = contact.canonical_job_id
              ORDER BY created_at DESC   LIMIT 1

profile_id = application.profile_id if application else default_profile.id
return CorrelationResult(profile_id, application, contact, job)
```

If no `is_default=True` profile exists (fresh DB before `seed`), the agent logs
`no_default_profile` and does **not** open a session for that call (it retries
correlation on the next event). `communication_sessions.profile_id` is
`NOT NULL`; in normal operation `seed` guarantees a default profile.

Notes:

- `normalize_e164` is the shared helper extracted from
  `app/crawlers/adapters/rabota_md/adapter.py` into `app/phone/numbers.py`
  (parse → `is_valid_number` → `format_number(E164)`); the rabota adapter is
  updated to import it. It is idempotent on already-normalized input.
- Phase 1 matches only the scalar `SourceJob.public_phone`. Matching the
  `public_phones` JSON array and a proactive discovery pass over all jobs are
  deferred (Phase 1.x / Phase 2).
- A known caller number is context, never an allowlist. Unknown numbers still
  produce a session under the default profile.
- Correlation never verifies a contact for sending; `PHONE` contacts are
  `UNVERIFIED` and outside the send path entirely.

## 9. Health model and API

### 9.1 Components (Phase 1)

| component | healthy | degraded | unavailable | unknown |
| --- | --- | --- | --- | --- |
| `phonegate_transport` | last poll OK | — | last poll raised | never polled yet |
| `a14_daemon` | `connected and is_daemon_mode` | `connected` via ADB fallback | not `connected` | no status yet |
| `gsm_line` | daemon connected and `call_state` readable | — | daemon down | no status yet |
| `sim_account` | telemetry present | telemetry stale | — | no telemetry |
| `agent` | poll within `phone_health_stale_after_seconds` | — | — | stale/never |

`asr` / `tts` / `llm_realtime` / `sms` are not represented in Phase 1 (added when
exercised). `sim_account` is informational only. `channel` = worst of
`phonegate_transport`, `a14_daemon`, `gsm_line`, `agent`.

### 9.2 `GET /api/v1/phone/status`

`require_api_actor`, `get_session`. Reads `phone_channel_health` rows plus the
newest `CommunicationSession` for `current_call`.

```json
{
  "channel": "healthy",
  "agent": { "last_poll_at": "2026-09-02T10:15:03Z", "stale": false,
             "open_session_id": null },
  "components": [
    { "component": "phonegate_transport", "status": "healthy",
      "last_ok_at": "...", "detail": null },
    { "component": "a14_daemon", "status": "healthy", "detail": "Zero-ADB" }
  ],
  "device": { "daemon_version": "0.2.x", "battery": 87, "sim_operator": "Orange",
              "rx_audio_stats": { "captured_frames": 0, "queued_frames": 0,
                                  "dropped_frames": 0 } },
  "current_call": { "state": "idle", "caller_number": null }
}
```

`caller_number` in the API response is masked.

### 9.3 `GET /api/v1/phone/sessions` and `/sessions/{id}`

- List: recent sessions, `limit` (default 50, max 200), newest first; each row
  is `public_model(...)` with masked `remote_address`, outcome, timestamps,
  correlated `application_id` / `profile_id`, turn count.
- Detail: the session plus its `communication_turns` ordered by `seq`.

### 9.4 Admin — Диагностика

A "Phone channel" section inside the existing `view == "diagnostics"` branch of
`app/admin/routes.py::dashboard`:

- `_phone_health()` helper queries `phone_channel_health` + newest session.
- Template partial `app/admin/templates/_phone_health.html`: component list with
  status dots (reuse existing `status_tone` / dot styles), a daemon/SIM line,
  current call state, last-poll age.
- No new nav entry, no new top-level view. Degradation here does not change
  global readiness or any existing badge.

## 10. Error handling and edge cases

| case | behavior |
| --- | --- |
| PhoneGate unreachable | `phonegate_transport = unavailable`; loop keeps polling at idle cadence; no session mutation |
| PhoneGate restarts (`latest_id < cursor`) | resync (§7.5); open session, if any, closed `UNKNOWN` + `needs_review` unless a call is still active |
| Agent restart mid-call | reconcile (§7.5): keep session open if call active, else close `UNKNOWN` |
| Transcript before session start | synthetic session from `status`, `needs_review=true` |
| Missed call (RINGING→IDLE) | session `outcome=MISSED`, `answered_at` null |
| Caller number changes during RINGING | old session `ABANDONED`, new session opened |
| Withheld / empty caller number | `remote_address=""`, correlation → default profile |
| `IDLE` event lost | close from `status` next cycle, `outcome=UNKNOWN` + `needs_review` |
| Events gap (buffer eviction) | log `events_gap`; `needs_review=true` on open session |
| Duplicate transcript id on re-poll | unique constraint → upsert no-op |
| Two agent processes | second fails the Redis lock and exits |
| `phone_agent_enabled=false` | process exits 0; no DB writes |
| DB write fails mid-cycle | cycle rolls back; cursor not advanced past the failed batch; retried next cycle (idempotent) |

## 11. Testing strategy

### 11.1 Fake PhoneGate — `tests/fixtures/fake_phonegate.py`

In-process Starlette app implementing `/api/health`, `/api/device/status`,
`/api/events`, `/api/call/transcript`, driven by a scriptable timeline:

```python
fake = FakePhoneGate()
fake.ring(caller="+37360111222")
fake.answer()
fake.transcript(speaker="rx", text="Здравствуйте, по поводу вакансии", confidence=0.82)
fake.transcript(speaker="rx", text="в четверг в два часа")
fake.hangup()
fake.restart()          # resets event ids, keeps status
```

Mounted into `PhoneGateClient` via `httpx.ASGITransport`. Mirrors the
`fixture_site` pattern.

### 11.2 Unit

- `numbers.normalize_e164`: MD numbers, already-E.164 input, invalid, empty.
- `PhoneGateClient`: happy path, 4xx→`PhoneGateError`, transport→`PhoneGateUnavailable`,
  lenient schema parsing.
- `CallerCorrelation`: known contact; contact via `SourceJob.public_phone`;
  unknown → default profile; no default profile guard; withheld number.
- Event dispatch state machine: every transition and every §10 edge case, with
  an in-memory or SQLite session.
- Health computation: each component's status derivation; `channel` roll-up;
  staleness.

### 11.3 Integration (`tests/integration/`, service-backed Postgres)

- Full ingest loop against `FakePhoneGate` + real Postgres: scripted call →
  exactly one `CommunicationSession` with correct timestamps/outcome + N
  `CommunicationTurn` in order; correlation writes the right ids; health rows
  updated.
- Idempotency: run the same scripted timeline through two poll passes → no
  duplicate turns, no duplicate session.
- Restart mid-call: `fake.restart()` between polls → session closed/kept per
  §7.5.

### 11.4 Migration

`tests/integration/test_sqlite_migrations.py` covers upgrade; a dedicated test
asserts the five tables and `ContactType.PHONE` acceptance; `alembic check`
clean.

### 11.5 E2E (`tests/e2e/`)

Scripted inbound call through the real `IngestLoop` wired to `FakePhoneGate` and
a real DB, asserting the persisted graph and that `call_facts` /
`interview_appointments` exist but are empty. Optionally surfaced in
`scripts/demo-e2e.sh`.

### 11.6 Live smoke (opt-in, `-m live`)

`ENABLE_LIVE_PHONEGATE_SMOKE_TEST=true` → hit real PhoneGate `/api/health` and
`/api/device/status` only; assert shape. Never answers, speaks, or dials. Off in
CI.

### 11.7 Static

`ruff check .`, `ruff format --check .`, `mypy app` — `app/phone/` fully typed.

## 12. File-by-file change list

New:

- `app/phone/__init__.py`, `numbers.py`, `client.py`, `schemas.py`, `states.py`,
  `correlation.py`, `health.py`, `ingest.py`, `agent.py`
- `migrations/versions/<rev>_phone_call_agent_phase_1.py`
- `app/api/` — phone routes (new `app/api/phone_routes.py` or a section in
  `routes.py`; decide in planning)
- `app/admin/templates/_phone_health.html`
- `tests/fixtures/fake_phonegate.py`
- `tests/unit/test_phone_numbers.py`, `test_phone_client.py`,
  `test_phone_correlation.py`, `test_phone_ingest.py`, `test_phone_health.py`
- `tests/integration/test_phone_ingest_loop.py`
- `tests/e2e/test_phone_observer.py`

Modified:

- `app/models/enums.py` — new enums + `ContactType.PHONE`
- `app/models/entities.py` — five entities
- `app/settings/config.py` — settings block + production validator
- `app/cli.py` — `phone-agent` subcommand
- `app/api/routes.py` or `app/main.py` — mount phone router
- `app/admin/routes.py` — `_phone_health()` + Диагностика section
- `app/crawlers/adapters/rabota_md/adapter.py` — import shared `normalize_e164`
- `docker-compose.yml` (+ `docker-compose.prod.yml` if needed) — `call-agent`
  service
- `.env.example` — PhoneGate placeholders
- `README.md` — brief phone-agent note
- `docs/phonegate-call-agent-architecture.md` — mark Phase 1 status if desired

## 13. Deferred / follow-up

- UDS or Docker-bridge transport to PhoneGate (elegant same-host path).
- `public_phones` array matching + proactive phone-contact discovery pass.
- `/api/call/transcript` reconciliation for buffer-eviction gaps.
- Idle-mode for the Compose service when `phone_agent_enabled=false` (vs restart
  loop).
- Evidence clips, operator controls, auto-answer (Phase 2).

## 14. Open questions for planning

1. Compose service when disabled: exit-0 restart loop (simple) vs a dormant
   sleep-recheck mode (quieter). Lean: exit-0 + document, revisit if noisy.
2. Phone API routes: separate `app/api/phone_routes.py` module vs a section in
   the existing `routes.py`. Lean: separate module, mounted alongside.
3. One DB commit per poll cycle vs per event. Lean: per cycle, with the cursor
   advanced only after commit.
4. Redis singleton lock TTL and renewal cadence (reuse `leased_redis_lock`
   semantics from `app/scheduler/locks.py`).
5. Exact hook for extracting `normalize_e164` — new `app/phone/numbers.py` vs a
   shared `app/common/` location; confirm the rabota adapter's current helper
   signature and callers.
