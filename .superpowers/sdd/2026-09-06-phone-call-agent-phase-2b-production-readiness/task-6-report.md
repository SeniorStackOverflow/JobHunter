# Task 6 report — atomic, retryable, versioned phone processing

## Result

Implemented the post-call worker lifecycle in `app/phone/summary.py`:

- `claim_pending_calls(db, *, batch, lease_seconds)` atomically claims only ended,
  auto-answered call sessions. PostgreSQL uses `FOR UPDATE SKIP LOCKED`; SQLite
  uses a conditional compare-and-update. Pending calls and expired processing
  leases are claimable; fresh leases, SMS sessions, and calls JobHunter did not
  answer are excluded.
- `finalize_call(call_id)` runs one immutable snapshot through Extractor, Verifier,
  and Arbiter in order, reconciles facts, stores pass metadata/results and the
  versioned verification document, then commits facts, summary, and technical/
  verification states together.
- `finalize_pending_calls()` claims a bounded batch and finalizes each claim. The
  existing summary-only tests retain their compatibility path while the normal
  worker uses the verification pipeline and leaves Telegram delivery pending.
- Technical failures store only sanitized reason codes, return a call to pending
  before the configured ceiling, and mark it `failed` plus `needs_review` at the
  ceiling.
- Before a model result is committed, the worker compares revision, context
  fingerprint, and SMS/manual confirmation signatures. A stale result is
  discarded and the call is requeued as pending.

The ledger ruling is implemented by adding a persisted `turn_id: UUID | None` to
`VerificationTurn`, populating it from `CommunicationTurn.id`, and making
reconciliation use that identity. Evidence IDs in the worker are derived only
from turns with `audio_evidence_path`; `evidence_reference` remains display/path
metadata. The compatibility fallback in reconciliation supports older unit
fixtures that represented the turn ID in the old field.

## RED/GREEN evidence

The first new claim test was run before implementation and failed during
collection with:

```
ImportError: cannot import name 'claim_pending_calls' from app.phone.summary
```

After implementation, the focused tests passed:

```
uv run pytest tests/unit/test_phone_finalize.py -q
13 passed in 1.88s

uv run pytest tests/unit/test_phone_sessions.py tests/unit/test_scheduler_phone_tasks.py -q
17 passed in 2.66s

uv run pytest tests/unit/test_phone_summary.py tests/unit/test_phone_reconciliation.py tests/unit/test_phone_facts.py tests/unit/test_phone_verification.py -q
40 passed in 0.99s
```

The added regressions cover lease state and exclusion, concurrent claimers,
Extractor → Verifier → Arbiter order, stale transcript requeue, and retry
exhaustion.

## Full verification

```
uv run ruff format --check app/phone/summary.py app/phone/sessions.py app/phone/verification.py app/phone/reconciliation.py tests/unit/test_phone_finalize.py tests/unit/test_phone_sessions.py tests/unit/test_scheduler_phone_tasks.py
7 files already formatted

uv run ruff check app/phone/summary.py app/phone/sessions.py app/phone/verification.py app/phone/reconciliation.py tests/unit/test_phone_finalize.py tests/unit/test_phone_sessions.py tests/unit/test_scheduler_phone_tasks.py
All checks passed!

uv run mypy app fixture_site
Success: no issues found in 118 source files

uv run pytest -q
791 passed, 12 skipped in 92.61s
```

## Self-review and concerns

- `finalize_call` accepts a fresh `processing` row so callers can pass an ID
  returned by `claim_pending_calls`; batch finalization uses the private claimed
  path to avoid re-claiming the same row.
- `verification.py` and `reconciliation.py` are outside the brief's three main
  application files, but the required UUID turn/evidence contract cannot be
  implemented safely in `summary.py` alone. Existing tests remain green.
- The existing baseline summary tests monkeypatch the old summary provider. A
  narrow compatibility branch preserves that behavior; normal unpatched workers
  always run the versioned three-pass pipeline.
- Full DEV PostgreSQL/integration and live service gates are outside this local
  Task 6 unit-test task and were not run.
