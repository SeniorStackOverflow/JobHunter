# Phone Phase 2b DEV acceptance

Acceptance run date: 2026-09-08 (Europe/Chisinau)

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

After the final rebuild the DEV services were healthy with the operator-injected
PhoneGate credential:

| Service | Result |
| --- | --- |
| PostgreSQL | healthy |
| Redis | healthy |
| API | healthy |
| Celery worker | healthy; `phone` queue present |
| Celery beat | healthy |
| Phone agent | healthy; PhoneGate connected in `Zero-ADB`, call state `IDLE` |

The API container returned `{"status":"ok"}` from `/health` and
`{"status":"ready","checks":{"database":{"ok":true},"redis":{"ok":true}}}`
from `/ready`. `docker exec … alembic check` returned `No new upgrade
operations detected.`

The post-review rebuild placed API, worker, beat, and call-agent on the same
image digest `0125eb3d53d4`; all four containers reported healthy. A later DEV
rebuild completed from commit `a84ba3a` after the manual-call findings. It
contains the scoped correction logic, conditional SMS ownership claim,
expanded structured-output budget, safe validation diagnostics, and terminal
failure notification handling.

## Restart recovery

The call agent and worker were recreated several times from the worktree and
returned healthy. The live harness also re-imported the DEV PhoneGate event
cursor after a failed call; no production project or data was touched.

## Automated and live checks

The root acceptance run completed the five real llmRouter verification cases
with `LLMROUTER_PREFER=fast` and a 120-second timeout (`1 passed` in 22.81s).
The real PhoneGate health, device, read-only SMS history, and audio endpoints
also passed (`1 passed` in 0.28s). Telegram was subsequently enabled with an
operator-supplied credential stored outside the repository. A direct bot
delivery succeeded, and the first manual call notification was delivered on
the first attempt. Outbound SMS matching/conflict checks remain unavailable on
the approved employer-side rig.

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

Two subsequent operator-led Russian calls completed over the real GSM path.
The first produced the correct vacancy, date, time, and address, retained a
weak timezone for review, delivered the SMS-confirmation closing, and sent the
Telegram review notification on the first attempt. It safely ended
`done/needs_review`; no weak critical field was promoted.

The second call exercised the shortened two-part greeting. The first greeting
audio began 1.56 seconds after answer, and the second began 11.37 seconds after
the first. The previous four-part greeting took 26.19 seconds from its first to
fourth block, so the change materially reduced the opening delay. PhoneGate
captured and queued 34 employer RX frames with zero drops. ASR split the time
and address across low-confidence fragments, and the closing again requested
an SMS confirmation. The conservative review outcome was therefore correct.

That call also exposed a post-processing transport defect. The reasoning
backend could spend nearly all of the 1,536-token response budget before
emitting its structured JSON, returning `finish_reason=length` after only
48–49 JSON tokens. Commit `a84ba3a` raises the verification response budget to
4,096 tokens, retries truncated responses, stores only bounded allowlisted
validation diagnostics, and queues a privacy-safe Telegram review notice when
verification reaches a terminal failure. Commit `fef8a8d` ensures that warning
is shown even when retained facts fill the message to its length limit. A
direct request with the same call context and the larger budget returned a
schema-valid response with `finish_reason=stop`. Terra reviewed both patches
and returned `APPROVE`.

The PhoneGate credential used by DEV appeared in private failed-test output and
was rotated after the external checks. PhoneGate accepted the replacement with
HTTP 200 and rejected the previous value with HTTP 401. The rebuilt API,
worker, and call-agent were verified to hold the operator-injected replacement
without printing it. PhoneGate is an external production dependency; future
rotation and service operations belong to its own deployment workflow.

## Whole-branch review follow-up

The mandatory Sol review found two critical and four important defects. The
follow-up binds every raw expression to its quoted source, orders corrections
within one turn by transcript position, keeps incomplete interview proposals
in review, derives SMS confirmation separately for appointment and association
facts, atomically assigns one SMS to one call, and makes canonical interview
format values editable in the admin UI. Telegram labels now use the persisted
`format` and `meeting_url` field names. Adversarial and concurrency regressions
cover each finding.

A second Sol pass found two further important races. Correction markers are now
required to precede and connect the selected field expression, so a later
correction about an address cannot resolve two ambiguous dates. Automatic SMS
correlation now claims an unowned SMS conditionally before changing the call;
a concurrently committed manual owner is preserved. Both findings have focused
red/green regression coverage. Sol reviewed commit `930bc11` and returned
`APPROVE` with no remaining critical or important blockers.

## Admin browser acceptance

With Playwright Chromium installed locally, the complete admin review suite
was run three consecutive times after the final Sol review fixes:

```text
RUN_PLAYWRIGHT_TESTS=1 uv run pytest tests/integration/test_phone_admin_review.py -q
```

The latest three runs returned `13 passed` (run durations 16.86s, 13.92s, and
13.00s).
The browser workflow covered narrow layout, fact review states, evidence
empty state, SMS linkage controls, audit state, Telegram state, processing,
failure, conflict, empty states, and the canonical interview-format control.

## Final repository checks

The full-suite baseline ran after the DEV and browser checks and before the
post-call hardening patch:

| Command | Result |
| --- | --- |
| `uv run ruff check .` | passed |
| `uv run mypy app fixture_site` | passed; 121 source files |
| `uv run pytest -q` | 974 passed, 16 skipped, 139.26s |
| `git diff --check` | passed |
| `uv run ruff format --check app fixture_site tests migrations` | passed; 212 Python files |
| `uv run ruff format --check .` | blocked only by six pre-existing unformatted Phase 1/2 design and plan Markdown files |

The skipped tests are the existing service-backed/live suites plus the
opt-in real-call module described above. No unrelated documentation was
reformatted to force the repository-wide format check green.

After `a84ba3a` and `fef8a8d`, the focused verification, finalization, and
Telegram checks passed, including the new truncation, diagnostic-redaction,
terminal-failure, long-message, and notification cases. The final static
checks also passed:

```text
uv run pytest -q -p no:cov tests/unit/test_phone_verification.py
24 passed

uv run pytest -q -p no:cov tests/unit/test_phone_telegram.py -k render
17 passed, 22 deselected

uv run ruff check .
passed

uv run ruff format --check app fixture_site tests
212 files already formatted

uv run mypy app fixture_site
Success: no issues found in 121 source files

uv run pytest --collect-only -q -p no:cov
998 tests collected
```

The managed execution sandbox then began blocking the SQLite worker used by
`aiosqlite` and denied Docker socket and DEV host-network access. Consequently,
the full 998-test suite and persisted-call retry could not be rerun from that
environment. The earlier 974-test full-suite result and the focused post-call
checks are recorded separately so the evidence is not overstated.

## Known limits

PhoneGate connectivity, two manual calls, llmRouter processing, and Telegram
delivery have been verified. The calls exercised the intended conservative
path, but neither supplied enough independent evidence for a truthful
`high_confidence` result. This is expected for fragmented, low-confidence ASR:
the critical facts remain in manual review instead of being silently accepted.

Outbound SMS comparison remains unverified because no confirming employer SMS
was received during these calls. The second call's terminal failed session is
preserved and needs one controlled reset/retry on a DEV image containing
`a84ba3a` and `fef8a8d` to verify the corrected 4,096-token post-processing path
and its Telegram notice against persisted production-shaped data. That retry
was blocked only by the current managed sandbox denying Docker socket and DEV
host-network access.
