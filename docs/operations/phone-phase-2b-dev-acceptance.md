# Phone Phase 2b DEV acceptance

Acceptance run date: 2026-09-07 (Europe/Chisinau)

This artifact records the DEV checks completed from the `phone-2b-impl`
worktree. It contains no provider credentials, PhoneGate tokens, phone numbers,
raw SMS, or employer transcript.

## DEV Compose

The DEV project used the worktree `docker-compose.yml` and the local port
override at `/home/andrei/.config/jobhunter/docker-compose.dev.local.yml`.
The sibling checkout compose file was excluded after it was found to omit the
`phone` worker queue. The application image was rebuilt from this worktree;
the image identifier was recorded as `sha256:0ae26c6c…` and was created at
2026-09-07 13:27:19+03:00.

At 13:30:56 the rebuilt migration container failed because PostgreSQL rejected
the old default credentials. The retry used the existing DEV database
credentials through the local process environment only. The migration then
reached head `b7c8d9e0f1a2` and exited 0. During that upgrade PostgreSQL
reported that the generated self-referential foreign-key name exceeded its
63-character identifier limit; the migration now uses the bounded name
`fk_communication_sessions_related_session`.

At 13:36:28 the DEV services were healthy:

| Service | Result |
| --- | --- |
| PostgreSQL | healthy |
| Redis | healthy |
| API | healthy |
| Celery worker | healthy; `phone` queue present |
| Celery beat | healthy |
| Phone agent | healthy; dormant because DEV PhoneGate credentials were not configured |

The API container returned `{"status":"ok"}` from `/health` and
`{"status":"ready","checks":{"database":{"ok":true},"redis":{"ok":true}}}`
from `/ready`. `docker exec … alembic check` returned `No new upgrade
operations detected.`

## Restart recovery

The DEV worker was restarted at 13:33:19 and was healthy again at 13:33:34.
Its logs show a clean warm shutdown, Redis reconnect, and `ready` state. A
post-restart database check reported `duplicate_transport_groups|0` and
`pending_calls|0`. No real pending call existed in this run, so a duplicate
post-crash task delivery could not be exercised with live call data.

## Automated checks

The opt-in real-call module was executed with:

```text
uv run pytest -q -m realcall tests/realcall/test_realcall_phase_2b.py
```

Result: 4 skipped. The environment did not provide
`ENABLE_REALCALL_TESTS`, PhoneGate endpoint/authentication, llmRouter
credentials, Telegram credentials, or A06/A14 rig values. Consequently no
real llmRouter request, Telegram delivery, PhoneGate request, GSM call, or
SMS was attempted. Matching and conflicting SMS checks remain blocked on the
approved employer-side SMS rig; the harness intentionally has no outbound SMS
operation.

The local A06 harness checks passed:

```text
uv run pytest tests/unit/test_realcall_a06_rig.py tests/unit/test_realcall_preconditions.py -q
3 passed
```

## Task 12 llmRouter contract follow-up

On 2026-09-07 the authorized DEV environment was loaded for only
`LLMROUTER_BASE_URL`, `LLMROUTER_API_KEY`, `LLMROUTER_PREFER`, and
`OPENAI_MODEL`; no values were recorded here. The verification request boundary
now normalizes every Extractor, Verifier, Arbiter, and SMS JSON schema so every
object property is required. Defaulted fields are nullable in the wire schema
and remain accepted by the typed Pydantic result models. The completion budget
was raised from 1200 to 1536 after a reasoning backend returned truncated JSON.

The focused regression and the complete phone unit module passed:

```text
uv run pytest -q tests/unit/test_phone_verification.py
16 passed
```

An authorized direct Extractor request returned HTTP 200 with the normalized
schema. The initial full opt-in run still encountered one sanitized HTTP 400;
the repeated five-fixture run no longer showed the original schema rejection,
completed the absolute fixture's Extractor and Verifier passes, then stopped on
an llmRouter timeout in the Arbiter pass after 278.12 seconds. This is an
external provider-capacity limit in this run; the real llmRouter acceptance
therefore remains partial.
No provider response body, credential, transcript, or raw identifier is stored
in this artifact.

## Admin browser acceptance

With Playwright Chromium installed locally, the complete admin review suite
was run three consecutive times at 13:36:52, 13:37:05, and 13:37:19:

```text
RUN_PLAYWRIGHT_TESTS=1 uv run pytest tests/integration/test_phone_admin_review.py -q
```

Each run returned `9 passed` (run durations 10.45s, 10.66s, and 9.77s).
The browser workflow covered narrow layout, fact review states, evidence
empty state, SMS linkage controls, audit state, Telegram state, processing,
failure, conflict, and empty states.

## Final repository checks

The final checks ran after the DEV and browser checks:

| Command | Result |
| --- | --- |
| `uv run ruff check .` | passed |
| `uv run mypy app fixture_site` | passed; 120 source files |
| `uv run pytest -q` | 912 passed, 16 skipped, 135.56s |
| `git diff --check` | passed |
| `uv run ruff format --check .` | blocked by six pre-existing unformatted Phase 1/2 design and plan Markdown files; changed Python and acceptance files pass targeted format checks |

The skipped tests are the existing service-backed/live suites plus the
opt-in real-call module described above. No unrelated documentation was
reformatted to force the repository-wide format check green.

## Known limits

The live acceptance remains incomplete until the operator supplies the
authorized DEV Telegram and PhoneGate configuration and the approved SMS rig,
and until the llmRouter Arbiter pass completes within its timeout budget. The
final manual operator call is intentionally still pending and must occur only
after Sol review.
