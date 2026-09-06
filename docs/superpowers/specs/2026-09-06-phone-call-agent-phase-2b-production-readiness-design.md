# Phone Call Agent — Phase 2b production-readiness extension

Date: 2026-09-06

Status: approved design, awaiting implementation plan

Branch: `phone-2b-impl`

Base design: `docs/superpowers/specs/2026-09-05-phone-call-agent-phase-2b-design.md`

Parent architecture: `docs/phonegate-call-agent-architecture.md`

## 1. Decision and precedence

This document extends Phase 2b from a loose post-call summary into a Russian-only,
production-usable post-call verification workflow. It records decisions approved on
2026-09-06 after implementation of Tasks 1–14 from the original Phase 2b plan.

Where this document conflicts with the 2026-09-05 Phase 2b design, this document wins.
In particular, the following are now in Phase 2b:

- structured critical-field extraction and `CallFact` writes;
- deterministic normalization of Russian dates and times after the call;
- inbound SMS ingestion and correlation through PhoneGate;
- multi-stage LLM verification;
- a review queue and audited manual fact correction;
- Telegram delivery retries;
- a mandatory real DEV run with PhoneGate, llmRouter, Telegram, and an automated GSM
  call before handoff.

`InterviewAppointment` writes, calendar synchronization, realtime LLM dialogue,
autonomous scheduling, outbound calls, and outbound SMS remain outside Phase 2b.

## 2. Required outcome

At handoff, a real Russian employer call can be answered by the existing deterministic
Phase 2a flow, recorded, analyzed, verified, displayed in the admin panel, and reported
through Telegram. Critical values must never silently become trusted when the evidence
is ambiguous.

The realtime call remains intentionally simple:

1. play the fixed Russian greeting;
2. listen and persist employer turns;
3. capture best-effort evidence around important turns;
4. play a deterministic closing;
5. perform all LLM work later in Celery.

No LLM participates in realtime dialogue or chooses arbitrary speech.

## 3. Critical fields and trust states

Confirmation-critical fields are:

- interview date;
- interview time and timezone;
- interview format (`onsite`, `remote`, or `phone`);
- physical address or meeting link;

Company and vacancy are association-critical fields: they must safely associate the
call with an application, but the employer does not need to repeat them in the
confirmation SMS.

The existing `CallFact` entity is the source of truth for extracted values. Its states
are used as follows:

| `CallFact.state` | Meaning in Phase 2b |
|---|---|
| `candidate` | Independent model passes and evidence checks agree. Shown as `high_confidence`, never as externally confirmed. |
| `confirmed` | Confirmed by a correlated employer SMS or by an audited operator action. |
| `conflict` | Models, transcript passages, or SMS disagree. |
| `unknown` | The value cannot be recovered safely. |

Each fact also records its confirmation source (`sms` or `manual`) when confirmed.
Model consensus has no confirmation source and remains a candidate. Manual changes
record the operator, timestamp, old value, and new value in the audit log.

The session stores a derived `verification_status` for filtering:

- `confirmed`: every confirmation-critical fact mentioned in the call is confirmed,
  association-critical facts are at least safe candidates, and there are no conflicts;
- `high_confidence`: all present critical facts are safe candidates and there are no
  conflicts;
- `needs_review`: any critical fact is conflicting, unknown, incomplete, unsupported by
  evidence, or affected by a processing failure.

`summary_state` continues to represent technical processing state. A call may therefore
have `summary_state=done` and `verification_status=needs_review`.

### 3.1 Persistence extension

One additive Alembic revision updates the existing schema:

| Entity | Change |
|---|---|
| `CommunicationSession` | Add indexed `verification_status` (`not_applicable`, `pending`, `confirmed`, `high_confidence`, `needs_review`), `verification_revision` integer, nullable `processing_started_at`, nullable string `transport_external_id`, and indexed nullable self-FK `related_session_id`. Make `phonegate_event_id_start` nullable. |
| `CommunicationSession` constraint | Unique `(transport, channel, transport_external_id)`; multiple `NULL` values remain valid for call sessions. |
| `CallFact` | Add nullable `confirmation_source` (`sms` or `manual`) and `confirmed_at`; add unique `(session_id, field)` because the table stores the current fact revision. |
| `PhoneSummaryState` | Add `processing` for atomic worker claims. |

Competing model candidates, previous revisions, pass metadata, and review reasons are
stored under the versioned `summary["verification"]` document and in `AuditEvent`; the
current safe value remains queryable in `CallFact`. `confirmed_by_turn_id` points to the
SMS turn for SMS confirmation and remains null for a manual confirmation.

The migration uses the repository's SQLite-compatible batch pattern and performs
explicit backfills before making new status fields non-null.

## 4. Multi-stage post-call verification

Completed auto-answered calls are processed by a versioned, idempotent Celery pipeline.
The pipeline uses two independent passes and an adjudication pass:

1. **Extractor** receives the Russian transcript, the call timestamp, and
   `Europe/Chisinau`. It returns strict structured facts with raw expressions, exact
   transcript quotes, normalized values, and confidence.
2. **Verifier** receives the same original inputs and schema but cannot see the
   Extractor output.
3. Deterministic code validates the schemas, proves that claimed quotes occur in the
   stored transcript, normalizes values, and compares both outputs.
4. **Arbiter** receives both outputs and the transcript for every call containing
   critical information. It may accept a value only when a direct quote unambiguously
   supports it.

All llmRouter response models use strict Pydantic schemas with extra fields forbidden.
Provider, model, latency, attempt count, pipeline version, and sanitized failure details
are persisted for every pass.

Relative Russian dates are resolved from the call timestamp, never from the Celery run
time. The system preserves every competing expression. When the caller corrects a
value during the call, only the last unambiguous expression can win; earlier expressions
remain available for audit.

A fact can become `candidate/high_confidence` only when:

- Extractor and Verifier independently produce the same normalized value;
- every supporting quote exists in the persisted transcript;
- Arbiter accepts the value based on direct evidence;
- the relevant ASR turn is present and complete; explicit confidence below the
  configured quality floor fails the gate, while an unavailable confidence score uses
  conservative lexical and evidence checks;
- no other turn or SMS contradicts the value.

Unclear expressions such as “after lunch”, meaningful timezone ambiguity, truncated
ASR, missing evidence, conflicting corrections, or disagreement between passes yield
`needs_review`. Model confidence alone can never override a conflict.

## 5. SMS-request closing

The closing remains deterministic. When Russian transcript markers indicate a date,
time, address, meeting link, or interview arrangement, the assistant says:

> Спасибо. Чтобы избежать ошибки в дате и времени, пожалуйста, отправьте данные
> собеседования SMS на этот номер. Андрей свяжется с вами. Всего доброго.

If no critical marker is present, the existing closing remains unchanged. Failure to
select the SMS closing does not suppress post-call verification.

## 6. Inbound SMS persistence

PhoneGate already exposes normalized SMS history through `GET /api/sms` and a device
refresh through `POST /api/sms/sync`; no PhoneGate source modification is required.
JobHunter refreshes after startup/recovery, polls history from Celery, and stores
messages idempotently. This read/sync client capability never sends an outbound SMS.

Inbound SMS reuses the communication model:

- one `CommunicationSession(channel=sms, transport=phonegate)` per imported message;
- one employer `CommunicationTurn` containing the exact SMS text;
- a nullable self-reference from the SMS session to the related call session;
- a string `transport_external_id` for the PhoneGate message ID;
- uniqueness on `(transport, channel, transport_external_id)`.

`phonegate_event_id_start` becomes nullable: calls continue to use it, while SMS uses
the string transport ID rather than manufacturing an integer. SMS timestamps and phone
numbers are normalized before correlation.

Automatic correlation is allowed only when:

- the normalized remote number matches;
- the SMS timestamp is between five minutes before call end and 24 hours after it;
- exactly one eligible call exists in the window.

Multiple eligible calls are never guessed. The SMS is stored, left unlinked, and the
case is marked for review. The operator may link or unlink it in the admin panel.

The initial call result is published immediately. SMS ingestion runs about once per
minute and reprocesses linked calls when a new message arrives. A later matching SMS can
promote facts to `confirmed`; a contradiction changes them to `conflict` and reopens
review. Later SMS is treated as a possible clarification, not as an unaudited overwrite.

## 7. SMS semantic verification

The SMS text is compared with existing facts using a strict llmRouter response plus
deterministic checks for normalized dates, times, and addresses. A fact becomes
SMS-confirmed only when both semantic and deterministic checks agree.

The confirming SMS turn is referenced from the fact. A partial SMS confirms only the
fields it explicitly supports. Missing fields retain their prior state. A conflicting
SMS cannot be auto-resolved, even when its model confidence is high.

## 8. Admin workflow

The Calls section becomes the operational source of truth. It provides filters for:

- all calls;
- `confirmed`;
- `high_confidence`;
- `needs_review`;
- summary failures;
- Telegram delivery failures.

The call detail view presents each critical field with:

- normalized value and trust state;
- raw expression and exact transcript quote;
- Extractor, Verifier, and Arbiter decisions;
- correlated SMS evidence;
- the short WAV evidence clip for the relevant turn;
- the concrete reason for review.

Operator actions are:

- confirm a proposed value;
- correct a value;
- mark a value unknown;
- reject an incorrect SMS association;
- manually associate an SMS with a call.

All mutations use CSRF protection, validate normalized values server-side, and create an
audit event. The ordinary path requires no listening: confirmed and high-confidence
calls are usable immediately. A review case first exposes the exact disagreement and a
short evidence clip. Missing evidence is itself a review reason.

The REST list and detail endpoints expose the same verification status, facts, sources,
model metadata, Telegram state, and filters used by the admin UI. Caller numbers remain
masked in list/API/Telegram output under the existing privacy policy.

## 9. Telegram

Telegram sends a compact Russian result containing:

- verification status;
- company and vacancy;
- safe date, time, format, and address values;
- short supporting quotes;
- conflict reasons when review is required;
- an admin link.

Disputed values are labelled as alternatives rather than facts. Full caller numbers and
unnecessary personal data are excluded.

Delivery state is `pending`, `sent`, `retrying`, `failed`, or `disabled`. Temporary
failures use bounded exponential backoff and idempotent delivery records. Telegram is
never a source of truth and its failure does not change call, summary, or fact states.

## 10. Failure handling and idempotency

The technical post-call lifecycle is:

```text
pending -> processing -> done
                      -> failed
```

`processing` is added to `PhoneSummaryState` so workers can atomically claim a call.
The independent verification lifecycle is:

```text
pending -> confirmed
        -> high_confidence
        -> needs_review
```

Database locking prevents concurrent processing of one call. Every result is keyed by
call, pipeline version, and input revision. A retry updates the current revision rather
than creating duplicate facts, SMS sessions, or Telegram notifications. Transcript
changes and newly linked SMS create a new input revision and preserve prior decisions
in diagnostics/audit history.

Temporary llmRouter errors retry with bounded exponential backoff. Invalid JSON,
exhausted retries, or a permanent provider error leave the transcript intact, never
confirm critical fields, and make the call visible in `needs_review` with a sanitized
technical reason.

PhoneGate SMS polling failures do not modify earlier results. The admin health view
shows the degradation, and the next successful history poll imports missed messages.

Evidence capture remains best-effort and isolated from the realtime loop. Every
exception from evidence capture is contained, recorded without transcript or phone
leakage, and represented as missing evidence during verification.

## 11. Configuration and security

New thresholds, polling intervals, retry limits, correlation windows, and model routes
are typed settings with safe DEV defaults and documented `.env.example` entries.
Production validation requires all credentials for explicitly enabled integrations.

Secrets remain `SecretStr`, never appear in repr/logs/admin output, and are not written
to model metadata. PhoneGate and Telegram hosts follow the existing allowlist policy.
Raw employer transcript/SMS content is not logged. Evidence retention remains bounded
by age and total size.

Readiness continues to describe the core JobHunter service. Integration degradation is
visible in component health and the Calls UI without causing the web readiness endpoint
to flap.

## 12. DEV acceptance

Phase 2b cannot be handed off based only on mocks. The following must pass:

1. `ruff check .`, `ruff format --check .`, `mypy app fixture_site`, and full `pytest`.
2. `alembic upgrade head && alembic check` against real DEV PostgreSQL.
3. DEV Compose startup for PostgreSQL, Redis, web, Celery, and phone-agent, including
   readiness, queue processing, restart recovery, and persistence checks.
4. Real llmRouter calls for all three stages using Russian cases covering absolute and
   relative dates, caller corrections, ambiguous expressions, and contradictions.
5. Real PhoneGate health, call events, evidence audio, SMS synchronization, idempotent
   re-import, and recovery after a controlled temporary failure.
6. Real Telegram messages for `confirmed`, `high_confidence`, and `needs_review`, with
   delivery state persisted and privacy checked.
7. An automated real GSM call through the existing A06 harness, followed by evidence,
   post-processing, Telegram, and admin verification.
8. A real matching SMS that promotes facts to `confirmed`, and a conflicting test SMS
   that produces `needs_review`.
9. Admin verification of lists, filters, detail, evidence playback, SMS linking, manual
   correction, and audit history. Integration-sensitive Playwright scenarios must pass
   three consecutive times.

CI continues to use `FakePhoneGate` and `httpx.MockTransport`; live tests remain opt-in
and never run as part of ordinary CI.

After every automated and real DEV check passes and branch review is complete, the
operator performs one manual real phone call as the final acceptance scenario.

## 13. Implementation and review workflow

Work follows the requested agent assignments:

- `gpt-5.6-luna`: task implementation;
- `gpt-5.6-terra`: review of each task and thorough admin verification;
- `gpt-5.6-sol`: final whole-branch review;
- `gpt-6-astra`: escalation for complex architectural or correctness problems.

Each implementation task is reviewed before the next dependent task proceeds. Review
findings are verified against code and tests before being applied. The final branch
review occurs only after all local, service-backed, live-integration, and admin checks
are green.

## 14. Explicit non-goals

- realtime LLM dialogue or follow-up questions;
- Romanian or English call handling in this phase;
- automatic calendar or `InterviewAppointment` mutation;
- autonomous acceptance of an interview slot;
- outbound calls or automatic outbound SMS;
- PhoneGate source changes or service restart without separate operational need;
- production deployment as part of DEV acceptance.
