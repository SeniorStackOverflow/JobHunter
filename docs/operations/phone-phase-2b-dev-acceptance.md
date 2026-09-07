# Phone Phase 2b DEV acceptance

Acceptance run date: 2026-09-07 (Europe/Chisinau)

This artifact records the DEV checks completed from the `phone-2b-impl`
worktree. It contains no provider credentials, PhoneGate tokens, phone numbers,
raw SMS, or employer transcript.

## DEV Compose

The DEV project used this worktree’s `docker-compose.yml`, the local port
override at `/home/andrei/.config/jobhunter/docker-compose.dev.local.yml`, and a
secret-free temporary live override outside the repository. The sibling
checkout compose file was excluded because it omits the `phone` worker queue.
Only DEV containers were rebuilt and recreated. The migration reached head
`b7c8d9e0f1a2` and exited 0; PostgreSQL uses the bounded
`fk_communication_sessions_related_session` identifier.

The base and production Compose definitions now declare the durable named
volume `phone_evidence` at `/data/phone_evidence`. The call agent, DEV worker,
and production control worker mount it read-write; the API mounts it
read-only. `PHONE_EVIDENCE_DIR` is propagated to every relevant service. The
runtime image creates the directory for UID 10001 before the volume is first
used. Blank phone summary transport settings fall back to llmRouter base URL,
key, and model; explicit phone overrides still win.

After the final rebuild and credential rotation the DEV services were healthy:

| Service | Result |
| --- | --- |
| PostgreSQL | healthy |
| Redis | healthy |
| API | healthy |
| Celery worker | healthy; `phone` queue present |
| Celery beat | healthy |
| Phone agent | healthy; connected to DEV PhoneGate |

The API container returned `{"status":"ok"}` from `/health` and
`{"status":"ready","checks":{"database":{"ok":true},"redis":{"ok":true}}}`
from `/ready`. `docker exec … alembic check` returned `No new upgrade
operations detected.`

## Restart recovery

The call agent and worker were recreated several times from the worktree and
returned healthy. The live harness also re-imported the DEV PhoneGate event
cursor after a failed call; no production project or data was touched.

## Automated and live checks

The root acceptance run completed the five real llmRouter verification cases
with `LLMROUTER_PREFER=fast` and a 120-second timeout (`1 passed` in 22.81s).
The real PhoneGate health, device, read-only SMS history, and audio endpoints
also passed (`1 passed` in 0.28s). No Telegram credentials were present, so the
DEV override recorded Telegram as `disabled`; no message was sent or claimed.
Outbound SMS matching/conflict checks remain unavailable on the approved
employer-side rig.

The local A06 harness checks passed:

```text
uv run pytest tests/unit/test_realcall_a06_rig.py tests/unit/test_realcall_preconditions.py -q
3 passed
```

## Task 12 llmRouter contract follow-up

The verification request boundary normalizes every Extractor, Verifier,
Arbiter, and SMS JSON schema so every object property is required. Focused
phone verification tests passed. The real five-case result is recorded above;
no provider response body or credential is stored here.

```text
uv run pytest -q tests/unit/test_phone_verification.py
20 passed
```

No provider response body, credential, transcript, or raw identifier is stored
in this artifact.

## GSM acceptance observations

The authorized A06→A14 harness reached the DEV call agent multiple times. The
13:24 and 13:30 calls completed all three verification passes but correctly
ended `needs_review`: the first injected speech before `listening`, and the
second exposed global PhoneGate transcript IDs being used as a per-call cursor.
The latter also exposed ASR time `14.30`, which is now normalized safely to
`14:30` with ambiguity tests.  Calendar-like continuations such as
`12.09.2026` are rejected as times, while sentence punctuation after a dotted
time remains supported.  The real llmRouter fixture harness requires a
correction to select only the normalized Tuesday value or remain explicitly
unsafe, and checks that a conflicting address has no accepted arbitration
decision.

The 13:45 call was `skipped` because the rig produced no employer RX turn;
PhoneGate had only assistant transcript events for that call. A 14:07 retry
failed before injection because the rig’s Edge-TTS returned `NoAudioReceived`.
The subsequent retry completed the scripted call but again produced no RX
turn, so it was skipped. These runs do not constitute a high-confidence GSM
acceptance and are not represented as one. The harness now waits for
`listening`, seeds a per-call transcript cursor, recovers delayed post-IDLE
transcript events by bounded event time, and requires evidence links before
high-confidence facts can pass.

The final automated A06→A14 call completed the physical call, persisted an
employer RX turn and audio evidence, and ran Extractor, Verifier, and Arbiter.
It safely ended `done/needs_review`. Extractor and Arbiter agreed on the date,
time, address, timezone, and vacancy, but the independent Verifier labelled the
date and time as `meeting_url`; the reconciliation gate therefore refused to
promote those critical fields. The provider boundary now rejects structurally
impossible canonical values (for example, a time stored as a meeting URL) and
retries the independent pass. A direct request to the real llmRouter using the
same Russian phrase passed after this change with five correctly typed fields
on the first attempt. The reconciliation rule still requires two independent
passes plus transcript/audio evidence and matching Arbiter confirmation.

The DEV PhoneGate token that appeared in private failed-test output was rotated
after the external checks. PhoneGate accepted the new token with HTTP 200 and
rejected the old token with HTTP 401. The rebuilt API, worker, and call-agent
were verified to hold the current token without printing it.

## Whole-branch review follow-up

The mandatory Sol review found two critical and four important defects. The
follow-up binds every raw expression to its quoted source, orders corrections
within one turn by transcript position, keeps incomplete interview proposals
in review, derives SMS confirmation separately for appointment and association
facts, atomically assigns one SMS to one call, and makes canonical interview
format values editable in the admin UI. Telegram labels now use the persisted
`format` and `meeting_url` field names. Adversarial and concurrency regressions
cover each finding.

## Admin browser acceptance

With Playwright Chromium installed locally, the complete admin review suite
was run three consecutive times after the final Sol review fixes:

```text
RUN_PLAYWRIGHT_TESTS=1 uv run pytest tests/integration/test_phone_admin_review.py -q
```

Each run returned `13 passed` (run durations 13.86s, 13.69s, and 13.13s).
The browser workflow covered narrow layout, fact review states, evidence
empty state, SMS linkage controls, audit state, Telegram state, processing,
failure, conflict, empty states, and the canonical interview-format control.

## Final repository checks

The final checks ran after the DEV and browser checks:

| Command | Result |
| --- | --- |
| `uv run ruff check .` | passed |
| `uv run mypy app fixture_site` | passed; 121 source files |
| `uv run pytest -q` | 972 passed, 16 skipped, 135.47s |
| `git diff --check` | passed |
| `uv run ruff format --check app fixture_site tests migrations` | passed; 212 Python files |
| `uv run ruff format --check .` | blocked only by six pre-existing unformatted Phase 1/2 design and plan Markdown files |

The skipped tests are the existing service-backed/live suites plus the
opt-in real-call module described above. No unrelated documentation was
reformatted to force the repository-wide format check green.

## Known limits

Telegram delivery and outbound SMS comparison remain unverified because the
authorized credentials/rig were unavailable. The automated GSM run proves the
real call, ASR, evidence, persistence, and conservative review path. A fresh
manual GSM call is still required to record a truthful post-fix
`high_confidence` result; it must be performed only after the whole-branch Sol
review is clean.
