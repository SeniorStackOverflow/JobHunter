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

## Known limits

The live acceptance remains incomplete until the operator supplies the
authorized DEV llmRouter, Telegram, and PhoneGate configuration and the
approved SMS rig. The final manual operator call is intentionally still
pending and must occur only after Sol review.
