# Phone Call Agent — Phase 2b design

Date: 2026-09-05
Status: approved design, ready for implementation planning

> Superseded for Phase 2b delivery by
> `docs/superpowers/specs/2026-09-06-phone-call-agent-phase-2b-production-readiness-design.md`.
> The production-readiness specification governs verification states, critical-field
> normalization, inbound SMS, Telegram retries, review controls, and DEV acceptance.
Parent architecture: `docs/phonegate-call-agent-architecture.md` (§16, §24, §25, §26, §33, §34.2, §39, §40.2, §40.3)
Parent spec: `docs/superpowers/specs/2026-09-03-phone-call-agent-phase-2-design.md`
(§1.3 and §14 fix Phase 2b's boundaries; this spec fills them in)
Predecessor: Phase 2a merged to `main` (`b160176`, "merge: phone call agent phase 2a")
PhoneGate: `/srv/phonegate` (live service, own git repo). Phase 2b needs **no** PhoneGate
source changes — `GET /api/call/audio?seconds=N` already exists
(`/srv/phonegate/src/web/server.py:1335`; N=1..10, mono 16 kHz 16‑bit WAV, live rolling
buffer captured only while `IN_CALL`, `409` when the buffer is empty).

---

## 1. Context and scope

Phase 2a is the first time JobHunter answers real employer calls and speaks a fixed,
pre‑scripted Russian half‑duplex greeting, then listens/records, then plays a fixed
closing. No LLM, no fact extraction, no scheduling. Phase 2b is **additive** on top of
2a and adds three things:

1. **Evidence audio clips** — during an answered call, when an "important" employer
   utterance is transcribed, the orchestrator immediately fetches the last few seconds
   of GSM downlink audio and stores a short WAV for later manual ASR verification.
2. **Post‑call summary** — a Celery worker job summarizes the finished call transcript
   with an LLM (`llmRouter`, `prefer=quality`) into a short Russian paragraph plus loose
   string hints, and best‑effort notifies the operator over Telegram.
3. **`Звонки` admin section** — a new top‑level panel view (`?view=calls`) with three
   tabs (`Live` / `История` / `Evidence`) surfacing the above.

### 1.1 In scope

- `app/phone/client.py`: `recent_call_audio(seconds) -> bytes`.
- `app/phone/evidence.py` *(new)*: `EvidenceCapturer` (deterministic importance
  heuristic + bounded mid‑call fetch + atomic file write + per‑call cap) and
  `prune_phone_evidence()` (age + total‑size retention).
- `app/phone/summary.py` *(new)*: `PhoneSummaryProvider` (`llmRouter` OpenAI‑chat client
  with a strict `CallSummary` schema, its own settings), `finalize_pending_calls()`
  (evidence linking + LLM summary + Telegram notify), `_notify()`.
- `app/phone/telegram.py` *(new)*: `send_telegram_message(...)`, `TelegramDeliveryError`.
- `app/phone/orchestrator.py`: one hook that calls `EvidenceCapturer.maybe_capture(...)`
  in the `LISTENING` loop.
- `app/phone/sessions.py`: `close()` sets `summary_state='pending'` for an
  `auto_answered` session; `set_summary(...)`, `set_turn_evidence_path(...)`.
- `app/models/entities.py` + one additive Alembic migration:
  `communication_sessions.summary` (JSON), `communication_sessions.summary_state`
  (new enum `PhoneSummaryState`), `communication_turns.audio_evidence_path` (str).
- `app/scheduler/tasks.py` + `app/scheduler/celery_app.py`: two thin task wrappers,
  a new beat schedule, a new `phone` queue.
- `app/settings/config.py`: `phone_evidence_*`, `phone_summary_llm_*`, `telegram_*`;
  production validation for `telegram_enabled`.
- `app/admin/phone_routes.py` *(new)*: move the existing phone admin handlers
  (`_phone_health`, `phone_auto_answer_toggle`, `phone_call_action`) here, add
  `build_calls_context(...)` and the evidence WAV stream endpoint.
- `app/admin/templates/calls/*.html` *(new)* + `Звонки` nav link + badge in
  `base.html`; `dashboard()` gains `elif view == "calls":`.
- `app/api/phone_routes.py`: extend the existing `GET /api/v1/phone/sessions` and
  `/sessions/{id}` with the 2b fields + list filters.
- `FakePhoneGate` gains `GET /api/call/audio` + scripting helpers.
- Unit + integration tests on `FakePhoneGate` / `httpx.MockTransport` (CI); one
  optional real‑call assertion (opt‑in, never CI).

### 1.2 Out of scope (Phases 3–5, unchanged from the Phase 2 spec)

- Any LLM‑driven realtime dialogue, `dialogue_act` / `ResponseRenderer`, ASR‑trust
  classification, targeted repair.
- `CallFact` extraction / `call_facts` table (stays empty); `AvailabilityService`,
  calendar, relative‑date resolution, `InterviewAppointment` writes / the
  "Собеседования" tab as a real view; critical read‑back; autonomous slot confirmation.
- The realtime `call-realtime` model profile and `phone_llm_models` (§19) — that list
  (Groq `qwen3.8-27b`, Cloudflare `llama-4-scout`, Gemini `3.1-flash-lite`) is for the
  Phase 3 realtime path, **not** the post‑call summarizer.
- True barge‑in; the automated/scheduled A06 canary; latency histograms; outbound
  calls / SMS (`dial` / `send_sms` are still never added to the client).
- Telegram retry/queue infrastructure (2b: `sent` / `failed` / `disabled`, no retry).
- The Live tab's fact panel, trust classes, LLM route history, latency charts,
  operator free‑text `Speak`, `§39.13` browser refresh model.

---

## 2. Architecture

Three independent blocks on different parts of the pipeline. Do not combine them.

```
                         ┌─ mid-call, synchronous, best-effort ───────────────┐
CallOrchestrator ────────┤ evidence: important rx line → GET /api/call/audio  │
(job-agent phone-agent)  │   → WAV into storage/phone_evidence/<sid>/<tid>.wav │
                         └────────────────────────────────────────────────────┘

IngestLoop closes the session (IDLE) ──> if auto_answered: summary_state = 'pending'

Celery worker (never the phone-agent process — architecture §40.2):
  finalize-pending-calls   every 120 s   ─> 1. link evidence clips to turns  (always)
                                            2. PhoneSummaryProvider (llmRouter, quality)
                                               → communication_sessions.summary
                                            3. best-effort Telegram
  prune-phone-evidence     daily 03:40    ─> delete WAVs by age / total size

Admin: new ?view=calls (Live / История / Evidence) + GET /api/v1/phone/calls[/{id}]
```

**Key separation (architecture §40.2):** the live path (orchestrator) stays inside the
`job-agent phone-agent` process; all post‑call work runs in the Celery worker. The
orchestrator touches the audio buffer synchronously but under a hard timeout and a
`try/except` that swallows everything — an evidence failure never delays or aborts a
call and never changes the 2a state‑machine timings.

**Trigger, not `.delay()`:** the async phone‑agent does not enqueue Celery work. A
closed `auto_answered` session is marked `summary_state='pending'` and the beat scanner
picks it up within ~120 s. This is resilient to a phone‑agent restart and matches the
repo's existing polling pattern (`process_unprocessed_jobs`, `send_auto_approved_...`).

---

## 3. Persistence changes

One additive Alembic migration. `down_revision` = current head
(`e5f6a7b8c9d0`, phone phase 2a). `batch_alter_table` for SQLite; the two‑step
`add_column(server_default=…)` then `alter_column(server_default=None)` used by the 2a
migration.

### 3.1 `communication_sessions` — two columns

| Column | Type | Meaning |
|---|---|---|
| `summary` | `JSON`, `nullable=False`, `default=dict`, `server_default="{}"` then dropped | `{summary_text: str, hints: {mentioned_vacancy, proposed_datetime_text, proposed_address_text, contact_person_text, outcome_guess}, model_meta: {provider, model, latency_ms, attempts}, telegram: {state, sent_at, error}}`. `{}` = not summarized. |
| `summary_state` | `Enum(PhoneSummaryState, native_enum=False)`, `nullable=False`, `server_default="not_applicable"` then dropped | `not_applicable` (not auto‑answered / pre‑migration rows) · `pending` · `done` · `failed` · `skipped` (finalized without an LLM summary — disabled or no employer turns). Drives the beat scanner and the nav badge. |

### 3.2 `communication_turns` — one column

| Column | Type | Meaning |
|---|---|---|
| `audio_evidence_path` | `String(255)`, `nullable=True` | Relative path `<session_id>/<transcript_id>.wav` under `phone_evidence_dir`. `NULL` = no clip. Set by the evidence‑linking pass of `finalize_pending_calls`; nulled by `prune_phone_evidence`. (Architecture §24 calls this `audio_evidence_key`; the Phase 2 spec §1.3 fixes the name as `audio_evidence_path`.) |

### 3.3 New enum

`PhoneSummaryState(StrEnum)` in `app/models/enums.py`:
`NOT_APPLICABLE = "not_applicable"`, `PENDING = "pending"`, `DONE = "done"`,
`FAILED = "failed"`, `SKIPPED = "skipped"`.

### 3.4 `summary_state` lifecycle

- IngestLoop opens a session → `not_applicable` (default).
- `SessionStore.close()` → `if call.auto_answered: call.summary_state = PENDING`
  (never earlier — we do not summarize a call still in progress).
- `finalize_pending_calls`: selects `WHERE summary_state='pending' ORDER BY ended_at
  LIMIT phone_summary_batch`. For each:
  - evidence linking runs unconditionally (deterministic, no LLM);
  - if `not phone_summary_llm_enabled` **or** the session has no employer turns →
    `SKIPPED`;
  - LLM success → `DONE` + `summary` written;
  - `PhoneSummaryUnavailable` → `summary.model_meta.attempts += 1`; the row stays
    `PENDING` (retried next tick) until `attempts >= phone_summary_max_attempts`, then
    `FAILED`.
- `phone_summary_llm_enabled=false` never leaves rows stuck in a way that inflates the
  badge — the badge counts `needs_review` (and `failed`), not `pending`.

### 3.5 Migration‑head test assertions

Bump the head revision in `tests/integration/test_sqlite_migrations.py` and
`tests/unit/test_phone_migration.py`; extend the column/enum checks for the three new
columns and `PhoneSummaryState`.

---

## 4. Evidence clips — mid‑call capture

### 4.1 `EvidenceCapturer` (`app/phone/evidence.py`)

```python
class EvidenceCapturer:
    def __init__(
        self, *, client: PhoneGateClient, settings: Settings, session_id: UUID
    ) -> None: ...
    async def maybe_capture(self, rx_entries: list[TranscriptEntry]) -> None: ...  # never raises
```

Holds `self._captured: int`. Every exception is caught, logged as
`phone_evidence_capture_failed` with a `reason`, and does **not** count against the cap.

### 4.2 Orchestrator hook

`CallOrchestrator._drive()` already polls new transcript entries every
`phone_orchestrator_poll_seconds` (~0.15 s) inside the `LISTENING` loop. The hook is one
addition where `seen_transcript_id` is updated:

```python
if page.entries:
    seen_transcript_id = max(seen_transcript_id, max(e.id for e in page.entries))
    rx = [e for e in page.entries if e.speaker == "rx"]
    if rx:
        last_activity = now
        await self._evidence.maybe_capture(rx)
```

Only `LISTENING` — during `GREETING` the half‑duplex TX discards RX, so there is nothing
to capture. The `EvidenceCapturer` is constructed once per call in `CallOrchestrator.run`
(alongside `SessionStore`).

### 4.3 Importance heuristic — `_is_important(text: str) -> bool`

Deterministic, no LLM. `casefold`, then match if **any** of:

- contains a digit (`\d`);
- `len(text) >= phone_evidence_min_chars` (default 60);
- matches a module‑level `frozenset` / compiled regex of RU + RO tokens (rabota.md is
  Moldova; calls are in Russian or Romanian):
  - weekdays: `понедельник|вторник|…|пн|вт|…|luni|marți|…`
  - time: `час|часа|часов|утра|вечера|дня|полдень|ora|dimineața|seara`
  - address: `улиц|ул\.|бул|проспект|str\.|adresa|sector|офис|кабинет|каб\.`
  - interview: `собеседовани|интервью|встреч|резюме|CV|документ|приход|ждём|ждем|interviu`

### 4.4 Capture

- `phone_evidence_max_clips_per_call` (default 3; **`0` disables capture entirely**).
- `client.recent_call_audio(seconds=phone_evidence_seconds)` (default 8, clamped to
  PhoneGate's 1..10).
- PhoneGate returns a complete WAV (mono 16 kHz 16‑bit) — the bytes are written verbatim
  to `phone_evidence_dir/<session_id>/<transcript_id>.wav` via a temp file + `os.replace`
  (atomic). Size guard `phone_evidence_max_clip_bytes` (2 MiB; a 10 s clip ≈ 320 KiB).
- `<transcript_id>` = `entry.id` (PhoneGate transcript id) — stable, unique per session.
- Timing is approximate: the rx line surfaces after JobHunter's ASR (~0.4–1 s), and
  `/api/call/audio` returns "the last N seconds of the buffer as of now" ≈ the utterance
  plus its surroundings. This is not a frame‑accurate cut and the spec does not require
  one.
- Any failure (`409` empty buffer, `PhoneGateUnavailable`, `PhoneGateError`,
  `OSError`) → log + return; the cap is not consumed; the call is not affected.

### 4.5 `PhoneGateClient.recent_call_audio`

```python
async def recent_call_audio(self, seconds: int) -> bytes:
    # GET /api/call/audio?seconds=<clamped 1..10>
    # 200 → response.content (audio/wav bytes)
    # 409 → PhoneGateError (buffer empty / no active call)
    # >=500 → PhoneGateUnavailable ; other >=400 → PhoneGateError
```

A new binary path — `_get` currently assumes JSON, so this uses `self._client.get`
directly with the same status mapping. Still the **only** new client method; `dial` /
`send_sms` are not added.

### 4.6 Linking clip → turn (post‑call, deterministic)

The rx turn row is written by **IngestLoop**, which may not have committed it when the
orchestrator sees the line mid‑call. So the orchestrator only writes the file (fast FS
I/O, no DB round‑trip in the hot path). `finalize_pending_calls` links it after the
session closes (IngestLoop has committed all turns by then): for each `pending` session
it walks `phone_evidence_dir/<session_id>/`, and for each `<tid>.wav` sets
`communication_turns.audio_evidence_path` on the turn with the matching
`phonegate_transcript_id`. Linking is deterministic and runs **unconditionally**, even
when `phone_summary_llm_enabled=false`. An orphan file with no matching turn is left in
place and handled by `prune_phone_evidence`.

---

## 5. Post‑call summary

### 5.1 `PhoneSummaryProvider` (`app/phone/summary.py`)

Modelled on `LLMRouterProvider` (`app/matching/providers.py`) — an OpenAI‑chat‑compatible
`httpx` client to the local `llmRouter` with an `X-LLMRouter-Prefer` header — but with
**independent settings** (architecture §40.3):

| Setting | Default | Notes |
|---|---|---|
| `phone_summary_llm_enabled` | `false` | master switch for the LLM summary + Telegram |
| `phone_summary_llm_base_url` | `http://127.0.0.1:4000` | same local llmRouter as matching |
| `phone_summary_llm_api_key` | `None` → falls back to `llmrouter_api_key` | one local service, avoid configuring the key twice |
| `phone_summary_llm_model` | `""` | `LLMRouterProvider` rejects an empty `model`; when unset, fall back to `settings.openai_model`. If both are empty and `phone_summary_llm_enabled=true`, production validation errors ("an explicit summary model is required") — mirrors matching's `_provider_from_settings`. |
| `phone_summary_llm_prefer` | `"quality"` | architecture §25 "stronger smart/quality model" |
| `phone_summary_llm_timeout_seconds` | `60.0` | a background job, not latency‑sensitive; §40.3 explicitly allows a matching‑style long timeout here |
| `phone_summary_max_attempts` | `3` | after which the session goes `failed` |
| `phone_summary_batch` | `10` | sessions processed per beat tick |

```python
class PhoneSummaryProvider:
    def __init__(self, *, base_url, api_key, model, prefer, timeout_seconds) -> None: ...
    async def summarize(self, ctx: CallSummaryContext) -> CallSummary: ...
```

Invalid JSON / schema mismatch / timeout / transport error → `PhoneSummaryUnavailable`
(retryable). Never raises `MatchProvider*` errors; never touches `MatchResult`.

### 5.2 Model output schema — `CallSummary`

```python
class CallSummary(BaseModel):
    summary_text: str  # 2–4 Russian sentences
    mentioned_vacancy: str = ""
    proposed_datetime_text: str = ""  # verbatim from the caller — NOT normalized
    proposed_address_text: str = ""
    contact_person_text: str = ""
    outcome_guess: Literal[
        "interview_proposed", "info_request", "not_relevant", "unclear", "other"
    ] = "unclear"
    needs_review: bool = False
```

Persisted into `session.summary` as `{summary_text, hints: {mentioned_vacancy,
proposed_datetime_text, proposed_address_text, contact_person_text, outcome_guess},
model_meta: {provider, model, latency_ms, attempts}, telegram: {...}}`.
`session.needs_review = session.needs_review or result.needs_review` — only ever set up,
never cleared.

### 5.3 Model context — `CallSummaryContext` (architecture §25, §33)

- **transcript**: ordered turns — assistant turns use `spoken_text`, employer turns use
  `text`; caller number masked.
- **correlation**: company / vacancy title / application status, resolved from the
  session's `canonical_job_id` / `source_job_id` / `application_id` / `contact_id`.
- **candidate facts**: `UserProfile.confirmed_facts` (§33 — verified profile data only).
- **not** included: any raw PhoneGate control API, the PhoneGate token, unverified data.

Prompt (English instructions, Russian output): *summarize the employer call in 2–4
Russian sentences; extract loose string hints; do not invent facts; do not normalize
dates/times — copy the caller's wording; set `needs_review=true` on any ambiguity,
contradiction, or unclear outcome.* Strict output schema.

### 5.4 `finalize_pending_calls()` — service function

Pattern: an async function invoked by `_run_locked_periodic` (like
`app/reports/service.py`).

```
picked = sessions WHERE summary_state='pending' ORDER BY ended_at LIMIT phone_summary_batch
for session in picked:
    _link_evidence(session)                       # always (§4.6)
    if not settings.phone_summary_llm_enabled or session has no employer turns:
        session.summary_state = SKIPPED ; continue
    try:
        result = await provider.summarize(build_context(session))
    except PhoneSummaryUnavailable:
        attempts = session.summary.get("model_meta", {}).get("attempts", 0) + 1
        session.summary = {**session.summary, "model_meta": {**.., "attempts": attempts}}
        session.summary_state = FAILED if attempts >= phone_summary_max_attempts else PENDING
        continue
    session.summary = {summary_text, hints{...}, model_meta{provider, model, latency_ms, attempts}, telegram{state:"pending"}}
    session.needs_review = session.needs_review or result.needs_review
    session.summary_state = DONE
    await _notify(session, result)                 # §6
return {"picked": n, "done": .., "failed": .., "skipped": ..}
```

### 5.5 Celery wiring

- `app/scheduler/tasks.py`: `finalize_pending_calls_task` →
  `_run_locked_periodic("phone-finalize", finalize_pending_calls(), ttl_seconds=600)`.
- `app/scheduler/celery_app.py`: `beat_schedule["finalize-pending-calls"]` every
  `120.0` s, `options={"queue": "phone", "expires": 110}`; matching `task_routes` entry;
  a new `phone` queue added to the worker `-Q` list in `docker-compose.yml` /
  `docker-compose.prod.yml`.

---

## 6. Telegram notification (minimal)

### 6.1 Settings

| Setting | Default |
|---|---|
| `telegram_enabled` | `false` |
| `telegram_bot_token` | `SecretStr \| None` — added to the config secret‑repr list; never logged |
| `telegram_chat_id` | `str \| None` |

`validate_secure_production`: `telegram_enabled` ⇒ `telegram_bot_token` and
`telegram_chat_id` required (same shape as the existing `phone_agent_enabled` check).

### 6.2 `send_telegram_message` (`app/phone/telegram.py`)

```python
class TelegramDeliveryError(RuntimeError): ...

async def send_telegram_message(*, token: str, chat_id: str, text: str, timeout: float = 10.0) -> None:
    # POST https://api.telegram.org/bot<token>/sendMessage
    #   {chat_id, text, parse_mode: "HTML", disable_web_page_preview: true}
    # non-2xx / network error → TelegramDeliveryError
```

`api.telegram.org` is a fixed, hard‑coded host — not derived from user input or model
output — so there is no SSRF surface. Short notes added to `docs/security.md` and
`docs/threat-model.md` (new outbound channel; token handling; message contents).

### 6.3 Message templates (architecture §26)

Chosen by `result.needs_review` / `result.outcome_guess`. All interpolated values
HTML‑escaped. The caller number is **never** included; candidate `confirmed_facts` are
never included.

Confident (`needs_review=false`, `outcome_guess="interview_proposed"`):

```
📞 <company> — <vacancy>
<summary_text>
🕒 <proposed_datetime_text>
📍 <proposed_address_text>
🔗 <PUBLIC_BASE_URL>/?view=calls&session=<id>
```

Uncertain (`needs_review=true`):

```
📞 <company | "Неизвестная компания">
<summary_text>
⚠️ Требуется проверка — открой запись звонка.
🔗 <PUBLIC_BASE_URL>/?view=calls&session=<id>
```

The `🔗` line is omitted when `public_base_url` is unset.

### 6.4 Delivery + state (`_notify` inside `finalize_pending_calls`)

```python
if not settings.telegram_enabled:
    session.summary["telegram"] = {"state": "disabled"}
    return
try:
    await send_telegram_message(token=.., chat_id=.., text=render(result, session))
    session.summary["telegram"] = {"state": "sent", "sent_at": now.isoformat()}
except TelegramDeliveryError as exc:
    session.summary["telegram"] = {"state": "failed", "error": str(exc)}
    logger.warning("phone_telegram_delivery_failed", session_id=str(session.id), error=type(exc).__name__)
```

`_notify` **never raises**. A delivery failure leaves `summary_state='done'`, does not
touch the call/session state, and is **not retried** in 2b — `telegram.state="failed"`
is visible in the `Звонки` UI (§39.12) for the operator. (Future option: the scanner
re‑picks `summary_state='done' AND summary->telegram->>'state'='failed'` — noted, not
implemented.)

---

## 7. Evidence retention — `prune_phone_evidence`

### 7.1 Service (`app/phone/evidence.py`)

```
root = phone_evidence_dir ; cutoff = now - phone_evidence_retention_days
if not root.exists(): return {"removed": 0, "freed_bytes": 0}
clips = walk root → [(path, mtime, size)]                 # <session_id>/<transcript_id>.wav
remove = {c.path for c in clips if c.mtime < cutoff}      # 1. age
remaining = sorted(clips - remove, key=mtime asc) ; total = Σ size
while total > phone_evidence_max_total_mb * MiB and remaining:
    victim = remaining.pop(0) ; remove.add(victim.path) ; total -= victim.size   # 2. size
for path in remove:
    sid, tid = path.parent.name, path.stem
    UPDATE communication_turns SET audio_evidence_path = NULL
        WHERE session_id = :sid AND phonegate_transcript_id = :tid
    path.unlink(missing_ok=True)
remove now-empty <session_id>/ directories
commit
return {"removed": len(remove), "freed_bytes": ..}
```

A dangling pointer to a since‑deleted file is tolerated by the UI (the Evidence tab and
the WAV stream endpoint check file existence and show "запись недоступна").

### 7.2 Celery wiring

- `prune_phone_evidence_task` →
  `_run_locked_periodic("phone-evidence-prune", prune_phone_evidence(), ttl_seconds=900)`.
- `beat_schedule["prune-phone-evidence"]`: `crontab(minute=40, hour=3)` (03:40 UTC,
  off‑peak), `options={"queue": "phone"}` (same queue as `finalize`).

### 7.3 Evidence settings (all new)

| Setting | Default | Bounds |
|---|---|---|
| `phone_evidence_dir` | `"storage/phone_evidence"` | — |
| `phone_evidence_retention_days` | `30` | 1–365 |
| `phone_evidence_max_total_mb` | `500` | 10–10000 |
| `phone_evidence_seconds` | `8` | 1–10 |
| `phone_evidence_min_chars` | `60` | 10–500 |
| `phone_evidence_max_clips_per_call` | `3` | 0–20 (**0 = capture disabled**) |
| `phone_evidence_max_clip_bytes` | `2_097_152` | 65536–8388608 |

`storage/` is already git‑ignored.

---

## 8. `Звонки` admin section

### 8.1 Targeted refactor — `app/admin/phone_routes.py`

`app/admin/routes.py` is ~1780 lines with a single `dashboard()` function branching on
`view ==`. All phone‑admin code moves into a new `app/admin/phone_routes.py`:
`_phone_health()` (currently `routes.py:428`), the POST handlers
`phone_auto_answer_toggle` / `phone_call_action`, a new `build_calls_context(session,
tab, params)`, and the new evidence WAV stream endpoint. `dashboard()`'s
`elif view == "calls":` branch only calls `build_calls_context(...)`. The router include
is wired where the existing admin `router` is registered.

### 8.2 Navigation (architecture §39.1)

`base.html` sidebar gains a `Звонки` link (`/?view=calls`) with a `.nav-count` badge:

| Badge | Condition |
|---|---|
| red dot | phone channel `unavailable` or a critical phone alert |
| `1` | an active call (`CALL_OWNED_KEY` set) |
| `N` | phone sessions with `needs_review=true` (plus `summary_state='failed'`) |
| hidden | otherwise |

The count is a small query added to the `dashboard()` template context, like the other
nav counts.

### 8.3 Page `?view=calls&tab=live|history|evidence` (default `live`)

One `calls.html` extending `base.html`, one partial per tab, a compact phone‑health
strip on top (data from `_phone_health`; the detailed diagnostics stay in `Диагностика`
— architecture §39.1). The `Собеседования` tab from §39.1 is rendered as a disabled
stub ("доступно после Phase 4").

**Live tab (architecture §39.2, 2a‑level):**

- No call: the "Телефонная линия ● Готова" card — device summary + auto‑answer
  enabled/stopped badges + the stop/resume button (the current `_phone_health.html`
  auto‑answer block, relocated here).
- Active call: masked caller, company/vacancy, `Application #`, caller match confidence,
  `Telephony: CONNECTED`, `Сценарий: <script_stage>`, an append‑only transcript of the
  current session's turns (employer `text` / assistant `spoken_text`, each with
  timestamp + speaker; employer turns show `asr_confidence` when present), and the
  `Завершить` / `Замолчать` / `Остановить` buttons (the existing 2a POST endpoints).
  **No** `dialogue_act`, trust class, or LLM fields.
- Auto‑refresh: `<meta http-equiv="refresh" content="5">` while a call is active.

**История tab (architecture §39.5):**

- List of `CommunicationSession` with `channel=call` (both auto‑answered and Phase‑1
  observed), newest first, paginated via the existing `_pagination` helper.
- Row: date/time · company / vacancy (or "неизвестно") · direction · duration
  (`ended_at - started_at`) · outcome badge · `auto_answered` marker · `needs_review`
  flag · `summary_state` chip · Telegram chip.
- Filters (query param): `all` / `needs_review` / `interview_proposed`
  (`summary.hints.outcome_guess`) / `missed_dropped` (`outcome in {missed, abandoned}`)
  / `unknown_caller` (`application_id IS NULL`).
- Search: company / vacancy / masked phone (`ILIKE` over the joined fields +
  `remote_address`).
- Row click → session detail (`&session=<id>`).

**Session detail:**

- Header: masked caller, company/vacancy/application link, direction, times, outcome,
  `auto_answered`, `script_stage`, `needs_review`.
- Summary block: `summary.summary_text` + a small hints table (vacancy / datetime text /
  address text / contact / outcome_guess) + `model_meta` (provider/model/latency, muted)
  + the Telegram state chip.
- Full transcript: all turns in `seq` order. Employer = neutral; assistant = with a
  `delivery_status` chip. An employer turn with `audio_evidence_path` shows an inline
  `▶` player.
- `rx_frame_stats` + `diagnostics` — muted, collapsible.
- Session audit events (`AuditEvent WHERE entity_id = <session_id>`) — a muted list.
- **Not in 2b:** facts panel, LLM route/fallback history, latency‑metrics charts.

**Evidence tab (architecture §39.8):**

- Flat list of clips (`communication_turns WHERE audio_evidence_path IS NOT NULL`)
  grouped by session: `<company/date> · turn <seq> · <N>s · создан <date> · истекает
  <mtime + retention_days>`.
- Each: `▶ Play` · the turn's transcript text · `asr_confidence` · a link to the
  session detail.

### 8.4 Endpoints (`app/admin/phone_routes.py`)

- `GET /admin/phone/evidence/{session_id}/{transcript_id}.wav` — streams the WAV from
  `phone_evidence_dir`. Admin‑auth. `session_id` must parse as a UUID, `transcript_id`
  must match `\d+`, and the resolved path must be inside `phone_evidence_dir` (no
  traversal). Missing file → `404` + "запись недоступна". `Content-Type: audio/wav`.

### 8.5 API (`app/api/phone_routes.py`) — extend the existing endpoints

`GET /api/v1/phone/sessions` and `GET /api/v1/phone/sessions/{session_id}` already
exist. Extend them rather than add parallel `/calls` routes:

- list: add `auto_answered`, `script_stage`, `summary_state` to each row; add optional
  query filters `filter` (`all` / `needs_review` / `interview_proposed` /
  `missed_dropped` / `unknown_caller`) and `q` (company / vacancy / masked phone).
- detail: add `summary` (the full JSON), `summary_state`, and `audio_evidence_path`
  per turn.

Masked numbers throughout. **Decided (2026-09-05): in scope for this cycle.**

---

## 9. Testing

`filterwarnings=["error"]`, strict `mypy` on `app/`, pristine pytest output. No real
llmRouter / Telegram / PhoneGate in CI. Tests must not read the operator's `.env`
(existing autouse conftest fixture).

### 9.1 Unit

- `_is_important` — the full heuristic table (RU/RO tokens, digits, length, negatives).
- `EvidenceCapturer.maybe_capture` — cap respected; a fetch failure does not consume the
  cap; atomic write; `409` empty buffer swallowed; `max_clips_per_call=0` ⇒ no‑op.
- `PhoneGateClient.recent_call_audio` — 200 → bytes; 409 → `PhoneGateError`; 5xx →
  `PhoneGateUnavailable`; via `httpx.MockTransport`.
- `PhoneSummaryProvider.summarize` — valid JSON → `CallSummary`; bad JSON / schema /
  timeout → `PhoneSummaryUnavailable`; `X-LLMRouter-Prefer: quality` header sent.
- `finalize_pending_calls` — selects `pending` by `ended_at`; evidence linking always
  runs; LLM disabled / no employer turns → `skipped`; success → `done` + `summary` +
  `needs_review` only‑up; `PhoneSummaryUnavailable` → `attempts++` / `pending`; after
  `max_attempts` → `failed`; batch limit honoured.
- `_link_evidence` — matches by `phonegate_transcript_id`; an orphan file is left alone.
- `send_telegram_message` / `_notify` — 2xx ok; non‑2xx / network → `TelegramDeliveryError`;
  `disabled` → `state=disabled`, no HTTP; failure → `state=failed` + `error`, never
  raises, `summary_state` stays `done`.
- Telegram templates — confident vs uncertain; HTML escaping; caller number absent;
  `confirmed_facts` absent.
- `prune_phone_evidence` — age cutoff; size cap evicts oldest; DB pointer nulled; empty
  dirs removed; missing file tolerated; empty root → no‑op.
- `SessionStore.close` — `auto_answered` ⇒ `summary_state='pending'`; otherwise
  `not_applicable`.
- migration head bump + new columns/enum in `test_sqlite_migrations.py` +
  `test_phone_migration.py`.
- evidence stream endpoint — traversal / non‑UUID / non‑numeric rejected; path outside
  the dir rejected; missing file → 404; happy path → `audio/wav`.
- `build_calls_context` — tab routing, filters, pagination, search, badge count.
- `GET /api/v1/phone/calls[/{id}]` — masking, pagination, detail shape.

### 9.2 Integration (`FakePhoneGate`, CI)

- `FakePhoneGate` gains `GET /api/call/audio?seconds=N` → a synthetic WAV, `409` when
  configured empty; helpers `set_call_audio(pcm)`, `fail_next_audio()`.
- Orchestrator end‑to‑end: an auto‑answered call where the employer speaks a
  date/time line during `LISTENING` → a clip file appears under
  `storage/phone_evidence/<sid>/` → after close, `finalize_pending_calls` (llmRouter
  faked on `httpx.MockTransport` returning a canned `CallSummary`) links the clip, sets
  `audio_evidence_path`, transitions `summary_state` to `done`, `telegram.state` to
  `disabled`.
- Migration: `RUN_SERVICE_INTEGRATION_TESTS=1` `alembic upgrade head && alembic check`
  on DEV Postgres 16.

### 9.3 Real‑call harness (`tests/realcall/`, opt‑in, never CI)

- Extend the 2a happy‑path test: after the call, a clip WAV exists for the injected
  employer line; `finalize_pending_calls` (run inline) produces a non‑empty
  `summary_text` and links `audio_evidence_path`. **Skip with a clear reason** when
  `phone_summary_llm_enabled=false` or llmRouter is unreachable.
- This does **not** gate the 2b merge (unlike 2a's mandatory real call — 2b is additive
  and the evidence/summary paths are covered by the `FakePhoneGate` integration test).
  One real run is recommended before flipping `phone_summary_llm_enabled` /
  `telegram_enabled` in production.

### 9.4 "2b done" checklist

- `ruff check`, `ruff format --check` (feature files), `mypy app fixture_site`, full
  `pytest`, `alembic upgrade head && alembic check` on DEV Postgres, `docker compose
  config`.
- The `FakePhoneGate` evidence + summary integration test green.
- `.env.example` updated with the new settings as commented placeholders.

---

## 10. Invariants (carried into the plan)

1. `app/observability/health.py::readiness_status()` is unchanged — summary / evidence /
   Telegram degradation never affects `/ready` (architecture §12, §36).
2. `PhoneGateClient` gains **only** `recent_call_audio` — still never `dial` /
   `send_sms`.
3. Evidence capture is best‑effort and non‑blocking: any `EvidenceCapturer` exception is
   swallowed; the 2a state‑machine timings are untouched.
4. Post‑call work runs **only** in the Celery worker, never in the `job-agent
   phone-agent` process (architecture §40.2).
5. The post‑call model may summarize and may set `needs_review` (only up). It may **not**
   write `CallFact` / `InterviewAppointment` (they stay empty — Phases 3–4), may **not**
   normalize dates/times, may **not** promote a fact to confirmed (architecture §25,
   Phase 2 spec §14).
6. Caller numbers are masked everywhere new (`mask_phone`): history rows, session
   detail, evidence list, API responses, Telegram messages, logs, audit. `spoken_text`
   is assistant text (safe to store/show); employer transcript text follows the Phase 1
   handling (never logged raw).
7. Telegram: fixed host `api.telegram.org`; the bot token is a `SecretStr` in the
   secret‑repr list and is never logged; the message carries no candidate
   `confirmed_facts` and no raw caller number; a delivery failure never affects
   call / session / summary state (architecture §26).
8. Evidence retention is bounded and visible (age + total size); audio never leaves
   `storage/` + the DB (architecture §16, §33).
9. No real llmRouter / Telegram / PhoneGate in CI — `httpx.MockTransport` +
   `FakePhoneGate`. Real‑call + real‑LLM are opt‑in only.
10. Tests must not read the operator's `.env` (existing autouse conftest fixture).
11. DEV (`jobhunter-dev`, DEV Postgres `127.0.0.1:55432`) vs PROD (`jobhunter`,
    `/srv/jobhunter-prod` — hands‑off); never restart `/srv/phonegate` without the
    operator's explicit OK.
12. `from __future__ import annotations`; strict `mypy`; `filterwarnings=["error"]`;
    the ruff `select` set is unchanged.
13. Commit prefix `feat:` / `fix:` / `test:`; English commit messages. Follow the
    operator's current merge convention (Phase 2a landed as merge commit `b160176`).
14. `.env.example` gets the new settings as commented placeholders; production
    validation adds the `telegram_enabled` ⇒ token + chat_id rule (no new hard rule for
    the opt‑in summary path).

### Shipping defaults

| Flag | Default | Rationale |
|---|---|---|
| `phone_summary_llm_enabled` | `false` | an external LLM call; enabled deliberately |
| `telegram_enabled` | `false` | an external channel; needs a security review |
| `phone_evidence_max_clips_per_call` | `3` (on) | evidence runs only for auto‑answered calls, which are themselves off by default and gated by the real‑call canary; ≤3 short clips + 30‑day retention matches architecture §16 ("store sufficient evidence to audit"). The operator can set `0`. |

---

## 11. Open items

**Decided 2026-09-05:**

1. Evidence capture **on by default** (`phone_evidence_max_clips_per_call=3`) once
   auto‑answer is enabled. Operator can set `0`.
5. The thin `GET /api/v1/phone/calls[/{id}]` API is **in scope** for this cycle.

**Still open — operator may adjust; the plan proceeds with the spec defaults otherwise:**

2. The importance‑heuristic token set (§4.3) — enough, or add/remove tokens?
3. `phone_evidence_retention_days` (30) and `phone_evidence_max_total_mb` (500).
4. The Telegram message wording (§6.3).
