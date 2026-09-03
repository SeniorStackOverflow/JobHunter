# Phone Call Agent — Phase 2 design

Date: 2026-09-03
Status: approved design, ready for implementation planning
Parent architecture: `docs/phonegate-call-agent-architecture.md` (§6, §7, §8, §9, §16, §32, §34.2)
Predecessor: `docs/superpowers/specs/2026-09-02-phone-call-agent-phase-1-design.md`
(Phase 1 merged to `main`, tip `ec9d321`)
PhoneGate: `/srv/phonegate` (not a git repo, live service). Phase 2a needs no PhoneGate
source changes beyond the `boot_id` field already added and deployed in Phase 1's review pass.

---

## 1. Context and scope

Phase 1 shipped a **pure read-only observer** of PhoneGate: `job-agent phone-agent`
process, `app/phone/` package, correlation, transcript/session persistence, health, a
"Phone channel" admin panel. It never calls `answer`, `speak`, `dial`, or `send_sms`.

Phase 2 is **"deterministic greeting and evidence capture"** — the first time JobHunter
**answers real employer calls autonomously and speaks to the caller**. The speech is
entirely pre-scripted (fixed Russian phrase blocks); there is no LLM-driven dialogue,
no fact extraction, no scheduling authority. Those are Phases 3–5.

The dedicated GSM number receives only job-related calls, and employers may call from
numbers not listed in any vacancy. Therefore, when enabled, JobHunter answers **every**
inbound call (subject to an operator blocklist and a runtime stop).

### 1.1 Split: one spec, two implementation plans

This spec is the single source of truth for all of Phase 2. It is implemented in two
plans / SDD cycles:

- **Phase 2a — the core (risky; merged and field-tested first):**
  write transport (`answer` / `speak` / `hangup`), the speak-state fence and `/speak`
  idempotency, the auto-answer policy, the deterministic half-duplex call script
  ("greeting → listen → closing"), assistant-turn (`tx`) delivery reconciliation, and
  operator control (config flag + runtime stop + per-call actions). **Real-call tests
  against the live A14 gate are a required deliverable and gate the 2a merge.**
- **Phase 2b — additive (its own brainstorming + plan after 2a settles):**
  evidence audio clips (`GET /api/call/audio`), post-call LLM summary, and an expanded
  operator surface (the top-level `Звонки` section, §39 of the architecture).

### 1.2 In scope — Phase 2a

- Three new `PhoneGateClient` methods: `answer()`, `speak(text)`, `hangup()`.
- `app/phone/speak.py` — the `wait_until_speakable()` state fence and the `/speak`
  response handling (200 / 409 branches / ambiguous-timeout).
- `app/phone/policy.py` — `should_answer(...) -> AnswerDecision`, a pure function.
- `app/phone/orchestrator.py` — `CallOrchestrator`, drives one answered call through
  `POST_CONNECT_WAIT → GREETING → LISTENING → CLOSING → DONE`.
- `app/phone/script.py` — the fixed Russian phrase blocks as constants.
- Migration: `communication_turns` gains `delivery_status`, `spoken_text`;
  `communication_sessions` gains `auto_answered`, `script_stage`. New enum
  `TurnDeliveryStatus`.
- Settings: `phone_auto_answer_enabled` (default `false`), `phone_answer_blocklist`,
  and timing settings (§13).
- Runtime stop: Redis key + `POST /admin/phone/auto-answer/{stop|resume}`.
- Per-call operator actions: `POST /admin/phone/call/{session_id}/{hangup|mute}`.
- API: `GET /api/v1/phone/status` gains an `auto_answer` block and per-call
  `auto_answered` / `script_stage`.
- Admin "Phone channel" panel gains an "Авто-ответ" block with stop/resume and, on an
  active call, the script stage plus hangup/mute buttons.
- `FakePhoneGate` gains `answer` / `speak` / `hangup` and TX-state simulation.
- Unit + integration tests on `FakePhoneGate` (CI), plus the **real-call harness**
  `tests/realcall/` (opt-in, never CI) and a one-time manual acceptance call.

### 1.3 In scope — Phase 2b (deferred, sketched in §14)

- Evidence audio clips: `GET /api/call/audio?seconds=N` after "important" turns,
  bounded retention, `communication_turns.audio_evidence_path`.
- Post-call summary: a background (Celery) job that summarizes the transcript via an
  LLM and writes `communication_sessions.summary` plus free-text hints (mentioned
  vacancy, candidate date/time/address **as text only** — no `CallFact` /
  `InterviewAppointment` writes).
- The top-level `Звонки` admin section (§39) with history, transcripts, clips,
  summaries.

### 1.4 Out of scope — Phase 2 entirely (Phases 3–5)

- Any LLM-driven or free-form dialogue; `dialogue_act` / `ResponseRenderer`;
  ASR-trust classification; targeted repair.
- `CallFact` extraction and states; the `call_facts` table stays empty.
- `AvailabilityService`, calendar busy/free, relative-date resolver, critical
  read-back, autonomous interview-slot confirmation, `InterviewAppointment` writes.
- True barge-in (requires PhoneGate transport work — an interruptible TX primitive and
  RX-during-TX).
- The **automated** A06 GSM canary as a scheduled/CI job, latency histograms,
  provider-failover telemetry.
- Outbound calls and SMS; `dial()` / `send_sms()` are **not** added to the client.
- JobHunter-side TTS: `POST /api/call/speak` takes plain text and PhoneGate synthesizes
  (its own Piper) and plays it. JobHunter holds only fixed string constants.

---

## 2. Goals and non-goals

### Goals

- When `phone_auto_answer_enabled` is true and no stop is active, JobHunter answers
  every inbound call, plays a short deterministic Russian opening (reassurance +
  AI disclosure + scheduling-authority statement + one guided question), then listens
  and records, then plays a fixed closing and hangs up.
- Every autonomous `/speak` passes the `wait_until_speakable()` fence; an ambiguous
  `/speak` network failure is never blindly retried.
- Assistant turns are persisted with an honest delivery status; a `tx` transcript line
  means "attempted", not "spoken".
- The operator can stop auto-answer at runtime without a redeploy and can end or mute a
  specific active call.
- Any phone-channel failure degrades only the phone channel; the process never crashes
  and `/ready` is unaffected (architecture §12; `readiness_status()` must not change).
- The behavior is validated by **real GSM calls** against A14 before 2a merges.

### Non-goals

- Understanding what the caller said or reacting to it beyond capture.
- Confirming or scheduling anything.
- Sub-100 ms speak latency, barge-in, or full-call audio archival.
- Zero-touch production rollout — 2a ships with auto-answer **off by default**; the
  operator flips it on deliberately after the real-call canary is green.

---

## 3. Architecture — three independent state machines

Do not combine these (architecture §7).

### 3.1 Telephony state (from Phase 1, unchanged)

```
IDLE → RINGING → CONNECTED → ENDED
```

Derived from PhoneGate `call_state` (`RINGING`, `IN_CALL`, else `IDLE`). Phase 1's
`IngestLoop` owns this and the session lifecycle (open on `incoming_call` / `RINGING`,
close on `IDLE`).

### 3.2 Answer decision (new, transient)

```
EVALUATING → ANSWER | IGNORE(reason)
```

Computed once per inbound `RINGING` by `policy.should_answer(...)`. `IGNORE` means the
call is only observed (Phase 1 behavior). Both outcomes are audited.

### 3.3 Call-script state (new, one per answered call)

```
POST_CONNECT_WAIT → GREETING → LISTENING → CLOSING → DONE
                        │            │          │
                        └── on error / call drop / operator stop ──► ABORTED(kind)
```

Owned by `CallOrchestrator`. Persisted as `communication_sessions.script_stage`:
`greeting` · `listening` · `closing` · `greeting_completed` (terminal OK) ·
`aborted_operator` · `aborted_error` · `aborted_restart` (terminal not-OK).

At any instant the three are independent, e.g. `TelephonyState=CONNECTED`,
`ScriptState=LISTENING`.

---

## 4. The call script (Phase 2a)

### 4.1 Flow and timing

```
RINGING
  │  policy.should_answer(...) == ANSWER   (§5)
  ▼
POST /api/call/answer
  │  poll status until call_state == IN_CALL, bounded by
  │  phone_answer_connect_timeout_seconds (default 8). On timeout → ABORTED(error).
  ▼
POST_CONNECT_WAIT
  │  sleep phone_post_connect_wait_seconds (default 1.5). PhoneGate discards RX for
  │  ~1.2 s after IN_CALL and while tx_preparing; the first block must start after
  │  this window, never at t=0 (architecture §8).
  ▼
GREETING  (script_stage = "greeting")
  │  for each block in SCRIPT_GREETING (§4.2):
  │      wait_until_speakable()            (§6)
  │      POST /api/call/speak(block)
  │      persist assistant CommunicationTurn (delivery reconciliation, §6/§7)
  │      wait for TX idle, then a short inter-block listen gap
  │        (phone_inter_block_listen_seconds, default 0.8)
  ▼
LISTENING  (script_stage = "listening")
  │  poll transcript + status (fast, ~150 ms) for:
  │    IngestLoop keeps persisting rx turns in parallel — orchestrator does NOT
  │    duplicate that; it only watches for the exit conditions:
  │    • silence: no new transcript line for phone_listen_silence_timeout_seconds
  │      (default 20), measured from the later of (the last transcript line's
  │      occurred_at) and (the moment LISTENING was entered) — so a caller who
  │      says nothing at all is still given one full silence window before CLOSING
  │    • hard cap: elapsed since answer >= phone_call_hard_cap_seconds (default 180)
  │    • call_state left IN_CALL  → ABORTED(error) unless already closing
  │    • operator stop / per-call action  → see §10
  ▼
CLOSING  (script_stage = "closing")
  │  wait_until_speakable()
  │  POST /api/call/speak(SCRIPT_CLOSING)   (one block; a short variant on operator stop)
  │  wait for TX idle (bounded)
  ▼
POST /api/call/hangup
  ▼
DONE  (script_stage = "greeting_completed")
  │  IngestLoop closes the session on the next IDLE as in Phase 1
  │  (outcome = completed).
```

### 4.2 Phrase blocks (`app/phone/script.py`)

Fixed `str` constants, ordered. **Wording is the operator's to finalize during spec
review** — the below is the architecture-doc baseline (§8), split into short blocks so
the caller gets listening gaps between them (half-duplex, §4.3).

```
SCRIPT_GREETING = [
    "Здравствуйте. Если вы звоните по поводу вакансии или собеседования с Андреем — "
    "вы позвонили по адресу.",
    "Я — голосовой ассистент Андрея и помогаю согласовать собеседования от его имени.",
    "Важные дату, время и адрес я обязательно уточню и запишу.",
    "Подскажите, пожалуйста, по какой вакансии вы звоните?",
]

SCRIPT_CLOSING = (
    "Спасибо, я записал. Андрей свяжется с вами. Всего доброго."
)

SCRIPT_CLOSING_INTERRUPTED = (
    "Извините, мне нужно прервать разговор. Андрей свяжется с вами. Всего доброго."
)
```

No dynamic text in Phase 2a. Blocks are short (Piper synthesis and GSM quality both
degrade with length; architecture §8).

### 4.3 Half-duplex constraint

While PhoneGate TX is active, `on_daemon_audio_frame()` discards RX; there is no
`StopSpeech` / `CancelSpeech` primitive. Therefore:

- Blocks are transmitted one at a time, each followed by a wait for TX idle and a short
  listening gap.
- The orchestrator never tries to detect caller interruption during a block.
- Caller speech that overlaps a block is lost — accepted for Phase 2a.

---

## 5. Auto-answer policy (`app/phone/policy.py`)

Pure function, no side effects, no I/O beyond what the caller passes in:

```python
@dataclass(frozen=True, slots=True)
class AnswerDecision:
    answer: bool
    reason: str  # "disabled_by_config" | "stopped_by_operator" | "blocklisted"
                 # | "not_ringing" | "answer"

def should_answer(
    *,
    status: DeviceStatus,
    settings: Settings,
    runtime_stopped: bool,
    normalized_caller: str | None,   # E.164 or None
) -> AnswerDecision
```

Rules, first match wins:

| # | Condition | Decision |
|---|-----------|----------|
| 1 | `not settings.phone_auto_answer_enabled` | `IGNORE / disabled_by_config` |
| 2 | `runtime_stopped` | `IGNORE / stopped_by_operator` |
| 3 | `normalized_caller` in `settings.phone_answer_blocklist` | `IGNORE / blocklisted` |
| 4 | `status.call_state != "RINGING"` | `IGNORE / not_ringing` |
| 5 | otherwise | `ANSWER / answer` |

No correlation gate, no business-hours gate. Correlation still runs in Phase 1's
`IngestLoop` and populates the session; it does not influence the decision.

Every decision (both outcomes) is written to `AuditEvent` as
`communication.auto_answer_decision` with `{reason, caller: mask_phone(...)}`. The last
decision (reason + timestamp) is exposed in `GET /api/v1/phone/status`.

`phone_answer_blocklist` is a list of E.164 strings in settings, normalized on load.

---

## 6. Write transport and the speak-state fence

### 6.1 `PhoneGateClient` additions (`app/phone/client.py`)

```python
async def answer(self) -> None          # POST /api/call/answer
async def speak(self, text: str) -> None  # POST /api/call/speak  {"text": ...}
async def hangup(self) -> None          # POST /api/call/hangup
```

Transport only, no business policy. Same error mapping as the Phase 1 GET methods:
`>=500` → `PhoneGateUnavailable`; a `409` → `PhoneGateError` carrying the status code so
the caller can branch (`speak` needs the 409 detail). `dial()` / `send_sms()` are
**not** added.

### 6.2 `wait_until_speakable()` (`app/phone/speak.py`)

Before every autonomous `/speak`:

```
loop, bounded by phone_speak_fence_timeout_seconds (default 5), poll ~150 ms:
    read status
    call_state == IN_CALL and not tx_active and not tx_preparing   → return OK
    call_state in {IDLE, ENDED} / call ended                       → raise CallEnded
    otherwise (transitional / TX busy)                             → keep polling
  on timeout                                                       → raise SpeakFenceTimeout
```

After `answer()`, never call `speak()` immediately — the fence's `IN_CALL` wait plus
the `POST_CONNECT_WAIT` sleep cover the post-connect window.

### 6.3 `/speak` response handling

- **HTTP 200** — PhoneGate has written the assistant `tx` transcript line *before*
  synthesis/uplink are confirmed. Record the `CommunicationTurn` as
  `delivery_status = attempted` with `spoken_text = <block>`.
- **HTTP 409** — re-read status, then:
  - `IN_CALL` + TX busy → wait for TX idle (bounded), **one** retry.
  - transitional state → wait for `IN_CALL` (bounded), **one** retry.
  - `IDLE` / call ended → drop this block, no retry, record turn `failed`.
- **Network timeout (not a deterministic 409)** — delivery is ambiguous (PhoneGate may
  have accepted the utterance). **Do not retry** (a duplicate assistant utterance is
  worse than a missed one). Record the turn `delivery_status = delivery_unknown`, then
  resynchronize from PhoneGate TX/transcript state and continue with the next block.

### 6.4 Delivery reconciliation

A `tx` transcript line = "attempted", not "spoken". A turn moves
`attempted → delivered` only after the matching `tx_state` has returned to idle
following that attempt. If TX never activated for an attempted turn (observed by the
time the script advances or the call ends), mark it `failed`. The orchestrator performs
this reconciliation as it advances between blocks and once more at `DONE` / `ABORTED`.

---

## 7. Persistence changes

One additive Alembic migration, `down_revision = "d4e5f6a7b8c9"` (current head), 12-hex
id, `batch_alter_table` for SQLite compatibility.

### 7.1 `communication_turns`

- `delivery_status: Mapped[TurnDeliveryStatus]` — `mapped_column(enum_column(...),
  server_default="not_applicable", nullable=False)` then drop the server default.
  Values: `not_applicable` (inbound `rx` turns, and all pre-migration rows),
  `attempted`, `delivered`, `delivery_unknown`, `failed`.
- `spoken_text: Mapped[str | None] = mapped_column(Text)` — the exact text sent to
  `/speak` for an assistant turn.

`SessionStore.append_turn` already dedups on `(session_id, phonegate_transcript_id)`;
PhoneGate assigns transcript ids to `tx` lines too, so this is unchanged. A new
`SessionStore.record_assistant_turn(...)` helper writes the `tx` turn with
`speaker=ASSISTANT`, `spoken_text`, and an initial `delivery_status`, and a
`SessionStore.set_turn_delivery(turn_id, status)` updates it during reconciliation.

### 7.2 `communication_sessions`

- `auto_answered: Mapped[bool] = mapped_column(Boolean, server_default=false,
  nullable=False)` then drop the default — true iff `CallOrchestrator` issued
  `answer()` for this session.
- `script_stage: Mapped[str | None] = mapped_column(String(32))` — the terminal or
  in-progress script-state name (§3.3). `null` when the orchestrator never ran.

### 7.3 New enum

`TurnDeliveryStatus(StrEnum)` in `app/models/enums.py`:
`NOT_APPLICABLE = "not_applicable"`, `ATTEMPTED = "attempted"`,
`DELIVERED = "delivered"`, `DELIVERY_UNKNOWN = "delivery_unknown"`,
`FAILED = "failed"`. `TurnSpeaker.ASSISTANT` already exists.

Migration-head test assertions (`tests/integration/test_sqlite_migrations.py`,
`tests/unit/test_phone_migration.py`) are bumped and column checks extended.

---

## 8. Process architecture

The same `job-agent phone-agent` process and `call-agent` Compose service. Inside,
work is split by tempo:

### 8.1 `IngestLoop` (Phase 1) — the slow owner

Keeps ownership of: status/event polling, correlation, `rx` transcript persistence,
health, device snapshot, session open/close, the PhoneGate-restart generation logic.
Runs continuously. It gains one responsibility: when it sees `RINGING` and no
orchestrator owns the current call, it evaluates the policy and, on `ANSWER`, spawns
the orchestrator.

### 8.2 `CallOrchestrator` (new) — the fast, per-call driver

- Spawned by `IngestLoop` as a separate `asyncio` task, at most one at a time (there is
  only ever one call). A module-level flag / Redis key `job-agent:phone:call:owned`
  records ownership for the current call.
- Drives §4's flow: `answer` → post-connect wait → greeting → listening → closing →
  hangup. During an active call it polls status fast (~150 ms) for the fence and the
  exit conditions.
- **Reuses** `SessionStore` (via `record_assistant_turn` / `set_turn_delivery`) — it
  does not re-implement persistence. It does not persist `rx` turns; `IngestLoop` still
  does that in parallel.
- On completion (or call drop / exception), a `try/finally` clears ownership;
  `IngestLoop` then closes the session on the next `IDLE` exactly as in Phase 1.

### 8.3 Interaction rules

- The Phase 1 process singleton lock is unchanged.
- The Phase 1 Redis state key `job-agent:phone:events:state` is not touched by the
  orchestrator. Ownership and the runtime stop use their own keys
  (`job-agent:phone:call:owned`, `job-agent:phone:auto_answer_stopped`).
- While the orchestrator owns a call, `IngestLoop.run_cycle` continues but does not
  itself try to close/reconcile that session's script state.

---

## 9. Operator controls (Phase 2a)

### 9.1 Config flag

`phone_auto_answer_enabled: bool = False`. When false, `should_answer` returns
`IGNORE / disabled_by_config` and no orchestrator ever spawns. This is the coarse,
redeploy-scoped switch and the shipping default.

### 9.2 Runtime stop

Redis key `job-agent:phone:auto_answer_stopped` (`"1"` / absent).

- `POST /admin/phone/auto-answer/stop` sets it; `.../resume` clears it. Follows the
  existing admin action pattern (`/admin/pause/{paused}`, `/admin/sources/{id}/toggle`).
- `IngestLoop` reads it before spawning an orchestrator (`should_answer` rule 2).
- `CallOrchestrator` reads it (a) before `answer()`, (b) once per `LISTENING` poll. If
  it becomes set during an active call, the orchestrator goes to `CLOSING` with
  `SCRIPT_CLOSING_INTERRUPTED`, then `hangup`, `script_stage = aborted_operator`.

### 9.3 Per-call actions

On the active call, from the admin "Phone channel" panel:

- `POST /admin/phone/call/{session_id}/hangup` — immediate `hangup()`, orchestrator
  aborts, `script_stage = aborted_operator`. Session closed by `IngestLoop` on IDLE.
- `POST /admin/phone/call/{session_id}/mute` — the orchestrator stops sending any
  remaining *greeting* blocks and jumps straight to `LISTENING`. The call then ends
  through a normal `LISTENING` exit: silence timeout or hard cap → `CLOSING` plays the
  closing block as usual; an operator `hangup` → no closing. Recorded in session
  diagnostics as `mute_requested`.

Both validate that `session_id` is the currently-owned active call; otherwise `409`.
The orchestrator polls a small per-call command channel (Redis key
`job-agent:phone:call:cmd`) that these endpoints write.

### 9.4 API surface (`app/api/phone_routes.py`)

`GET /api/v1/phone/status` gains:

```json
"auto_answer": {
  "enabled": true,
  "stopped": false,
  "last_decision": {"answer": true, "reason": "answer", "at": "…"}
},
"current_call": { …existing…, "auto_answered": true, "script_stage": "listening" }
```

### 9.5 Admin panel (`app/admin/templates/_phone_health.html`)

A new "Авто-ответ" block: enabled/stopped badges + a stop/resume button. When a call is
active: the script stage, and hangup / mute buttons.

---

## 10. Failure and restart behavior

| Situation | Behavior |
|---|---|
| `answer()` returns 409 / `IN_CALL` not reached before `phone_answer_connect_timeout_seconds` | Orchestrator exits; `auto_answered=false`, `script_stage=null`. `IngestLoop` handles the session as an ordinary observed inbound (Phase 1). |
| `/speak` ambiguous timeout | Turn `delivery_unknown`; continue with the next block (no retry). |
| Call drops mid-greeting (`call_state` leaves `IN_CALL`) | Fence raises `CallEnded`; orchestrator exits; `script_stage=aborted_error`, `needs_review=true`. |
| PhoneGate unreachable during the call | Orchestrator exits after N failed polls; session `needs_review`; health → `degraded` (does not touch `/ready`). |
| **Process restart while an answered call is `IN_CALL`** | On startup, if `IN_CALL` and the current session has `auto_answered=true` and a non-terminal `script_stage`: **do not resume the script.** Switch to passive observe (Phase 1), set `script_stage=aborted_restart`, `needs_review=true`. Silence beats resuming a script mid-way. |
| Orchestrator task raises | `try/finally` clears ownership; `IngestLoop` closes the session on the next IDLE; error → audit + health. |

No phone-channel failure may crash the process or affect crawler/matching/Gmail/learning
or `/ready` (architecture §12; `app/observability/health.py::readiness_status()` must
not change).

---

## 11. Global constraints (carried into the plan)

- `from __future__ import annotations`; strict `mypy` on `app/`; pytest
  `filterwarnings=["error"]`; test output pristine.
- `PhoneGateClient` gains **only** `answer` / `speak` / `hangup` — never `dial` /
  `send_sms`.
- `app/observability/health.py::readiness_status()` unchanged; phone degradation must
  not affect `/ready`.
- Caller numbers masked (`+373••••NNN`) everywhere they surface — logs, `AuditEvent`
  details, API responses, admin templates. `spoken_text` is assistant text (safe to
  store/show); `rx` transcript text follows the Phase 1 handling.
- No real external calls / SMS in CI. `FakePhoneGate` and `httpx.MockTransport` only.
  Real-call tests are opt-in (`ENABLE_REALCALL_TESTS=true`), never run in CI.
- Tests must not read the operator's local `.env` (existing conftest autouse fixture).
- DEV vs PROD: DEV = compose project `jobhunter-dev`, DEV Postgres on
  `127.0.0.1:55432`. Never touch PROD project `jobhunter` / `/srv/jobhunter-prod`, and
  never restart/deploy `/srv/phonegate` without the operator's explicit OK.
- Migrations verified with `alembic upgrade head && alembic check` against DEV
  Postgres 16.
- `job-agent phone-agent` remains the process; `call-agent` remains the Compose
  service; `PHONE_AGENT_ENABLED=false` and `phone_auto_answer_enabled=false` are the
  shipping defaults.
- Commit prefix `feat:` / `fix:` / `test:`; English commit messages; the repo keeps a
  linear history (fast-forward merges, no merge commits).

---

## 12. Testing

### 12.1 Unit + integration on `FakePhoneGate` (CI)

`FakePhoneGate` (`tests/fixtures/fake_phonegate.py`) gains:

- `POST /api/call/answer` — `RINGING → IN_CALL`, emits a `call_state` event.
- `POST /api/call/speak` — appends a `tx` transcript line; sets `tx_preparing` →
  `tx_active` → idle on a test-controllable schedule; `409` when not `IN_CALL`.
- `POST /api/call/hangup` — `→ IDLE`, emits `call_state`.
- Scripting helpers: `fail_next_speak(mode="409_tx_busy" | "409_transitional" |
  "timeout")`, `advance_tx()` (force TX idle), `set_tx_state(...)`.

Coverage:

- `policy.should_answer` — the full rule table with reasons.
- `wait_until_speakable` — OK / TX busy keeps polling / call ended raises `CallEnded` /
  timeout raises `SpeakFenceTimeout`.
- `/speak` branches — one retry on 409, `delivery_unknown` on ambiguous timeout, no
  retry when ended.
- delivery reconciliation — `attempted → delivered` on TX idle; `attempted → failed`
  when TX never activates.
- `CallOrchestrator` end-to-end: happy path (`greeting_completed`, `tx` turns
  `delivered`, `rx` turn present); hard-cap cuts `LISTENING` → `CLOSING`; runtime stop
  mid-call → short closing → `aborted_operator`; per-call hangup / mute; call drop
  mid-greeting → `aborted_error` + `needs_review`; process restart → passive observe +
  `aborted_restart`; policy `IGNORE` (flag off / blocklist) → no orchestrator, call
  only observed.

### 12.2 Real-call harness — `tests/realcall/` (required deliverable, gates 2a merge)

`@pytest.mark.realcall`, opt-in via `ENABLE_REALCALL_TESTS=true`, **never in CI**.
Needs SSH + ADB to the VPS and phones. This is the "post-deploy call canary" of
architecture §32, run semi-automatically: the harness orchestrates **A06**, JobHunter
answers on **A14**, and assertions run against the recorded audio and the persisted
session.

Reuses the proven, immutable toolkit in
`/srv/phonegate/WORKING_DO_NOT_TOUCH_PROVEN/` (`a06_call_record.py` — SSH → `adb` over
Tailscale to A14 `100.106.163.104` / A06 `100.100.224.9`, digital `VOICE_DOWNLINK`
capture + Faster-Whisper; `CallStreamer` / `ParamSetter` — GSM uplink injection). The
harness **calls** these; it never edits that directory. The proven `a06_call_record.py`
dials A14→A06; Phase 2a needs the reverse (**A06 dials A14**), so the harness adds a new
wrapper `tests/realcall/a06_originate.py` built on the same primitives (`am start …
tel:`, `CallStreamer` for uplink, `ReceiverRecorder` src 3 for downlink) — a new file,
not a change to the protected set.

**`test_realcall_greeting_and_capture` (happy path):**

```
A06 dials A14's number
  → PhoneGate RINGING → CallOrchestrator policy ANSWER → POST /api/call/answer
  → A06 starts VOICE_DOWNLINK recording (src 3, 16-bit PCM)
  → JobHunter plays the greeting (4 blocks via /speak, PhoneGate Piper)
  → A06 injects a known WAV into the uplink:
       "Звоню по вакансии грузчика на склад"
  → A06 stays silent → LISTENING silence timeout → CLOSING → POST /api/call/hangup
  → A06 stops recording
```

Assertions:

1. **Downlink audio present** — duration within bounds, RMS/spectrum not silence,
   greeting and closing distinguishable (by inter-block pauses).
2. **ASR of the recording** (Faster-Whisper on the captured downlink) fuzzy-matches the
   script blocks above a similarity threshold — proves Piper synthesized and GSM
   downlink carried the right words.
3. **JobHunter RX worked** — a `CommunicationTurn` (speaker=`employer`) exists whose
   text fuzzy-matches the injected phrase — proves GSM uplink → A14 RX → Groq ASR →
   persist.
4. **Session** — `auto_answered=true`, `script_stage="greeting_completed"`,
   `outcome=completed`; the `tx` turns have `delivery_status="delivered"` and
   `spoken_text` equal to the script blocks.
5. **Hang-up** — `call_state` back to `IDLE`, `ended_at` set.

**Additional real-call scenarios (lighter assertions, same rig):**

- `test_realcall_runtime_stop_aborts` — set the Redis stop during the active call →
  downlink contains the short closing, hang-up occurs, `script_stage="aborted_operator"`.
- `test_realcall_per_call_hangup` — `POST /admin/phone/call/{id}/hangup` during the
  greeting → immediate hang-up, `script_stage="aborted_operator"`.
- `test_realcall_disabled_is_observed_only` — `phone_auto_answer_enabled=false` → A14
  does **not** auto-answer (A06 hears ringing), no orchestrator ran, but the inbound
  event is still recorded (Phase 1).

**Cost / frequency:** ~1–2 real minutes per run. Run before the 2a merge (mandatory,
green) and after significant orchestrator / transport changes. Not more often.

**Preconditions (the harness checks all and *skips with a clear reason* if unmet, never
fails):**

- SSH to the VPS (`46.225.103.75`); `adb` there sees A14 and A06 via Tailscale.
- A06: active SIM with minutes, screen unlocked, the proven `.dex` files present.
- A14: PhoneGate running, daemon connected (`connected=true`, `mode="Zero-ADB"`).
- JobHunter `call-agent` running with `phone_auto_answer_enabled=true` against the same
  PhoneGate.

### 12.3 Manual acceptance (one-time, before 2a merge)

The operator calls A14 from their own phone, hears the greeting live, says a couple of
sentences, hears the closing, the call ends. A human judgment: "does this sound
acceptable to a real employer?"

### 12.4 "2a is done" checklist additions

- `pytest -m realcall` green against the live A14 gate.
- Manual acceptance call passed.
- Existing sweep: `ruff check`, `ruff format --check` (feature files), `mypy app
  fixture_site`, full `pytest`, `alembic upgrade head && alembic check` on DEV
  Postgres, `docker compose config`.

---

## 13. Settings reference (all new)

| Setting | Default | Bounds | Purpose |
|---|---|---|---|
| `phone_auto_answer_enabled` | `false` | — | Master switch for autonomous answering (2a) |
| `phone_answer_blocklist` | `[]` | list[str] | E.164 numbers to never answer |
| `phone_answer_connect_timeout_seconds` | `8.0` | 2–30 | Max wait for `IN_CALL` after `answer()` |
| `phone_post_connect_wait_seconds` | `1.5` | 0.5–5 | Silence after `IN_CALL` before the first block |
| `phone_speak_fence_timeout_seconds` | `5.0` | 1–15 | Max wait in `wait_until_speakable()` |
| `phone_inter_block_listen_seconds` | `0.8` | 0.2–5 | Listening gap between greeting blocks |
| `phone_listen_silence_timeout_seconds` | `20.0` | 5–120 | Silence in `LISTENING` that triggers `CLOSING` |
| `phone_call_hard_cap_seconds` | `180.0` | 30–1800 | Absolute cap on an answered call |
| `phone_orchestrator_poll_seconds` | `0.15` | 0.05–1 | Fast poll cadence during an active call |

`validate_secure_production` gains no new hard rule; `phone_auto_answer_enabled=true`
in production is allowed (it is a deliberate operator choice), but it inherits the
existing Phase 1 checks (token present, non-loopback URL).

---

## 14. Phase 2b preview (not implemented here; boundaries only)

- **Evidence clips.** After an "important" `rx` turn (heuristic — e.g. a long line, or
  one containing date/time tokens), the orchestrator fetches
  `GET /api/call/audio?seconds=N` and stores a short WAV. Storage in a bounded volume
  (`resumes`-style, git-ignored), retention capped (e.g. 30 days), path in
  `communication_turns.audio_evidence_path`. Numbers masked in any log line.
- **Post-call summary.** After `DONE`, a Celery job takes the session transcript, calls
  an LLM (the current `LLMProvider.evaluate` is matching-specific — 2b adds a small
  general summarizer path), and writes `communication_sessions.summary` plus free-text
  hints (mentioned vacancy; candidate date/time/address **as strings**). No `CallFact` /
  `InterviewAppointment` writes — those are Phases 3–4.
- **Expanded operator surface.** The top-level `Звонки` admin section (architecture
  §39) with call history, transcripts, clips, and summaries.

2b gets its own brainstorming and plan. This spec fixes the boundaries only so 2a does
not foreclose them (hence `audio_evidence_path` is *not* added in 2a's migration — 2b
owns its schema).

---

## 15. Out of scope — Phase 2 entirely (Phases 3–5)

Free-form / LLM-driven dialogue; `dialogue_act` / `ResponseRenderer`; ASR-trust
classification; targeted repair; `CallFact` extraction and states; `AvailabilityService`
+ calendar; autonomous slot confirmation; `InterviewAppointment` writes; critical
read-back; true barge-in (needs PhoneGate transport work); the automated/scheduled A06
canary; latency histograms; provider-failover telemetry; outbound calls / SMS; the
top-level `Звонки` admin section.

---

## 16. Open items for the operator to finalize during spec review

1. **Exact phrase wording** (§4.2) — the greeting and closing text the employer hears.
2. **`phone_listen_silence_timeout_seconds`** default (20 s) — long enough that a
   thoughtful caller is not cut off, short enough that the call does not drag.
3. **`phone_call_hard_cap_seconds`** default (180 s).
4. Whether `test_realcall_disabled_is_observed_only` is worth the real minutes, or the
   "disabled" path is trusted to the `FakePhoneGate` integration test alone.
