# Task 4 report: strict independent llmRouter verification passes

## RED / GREEN

RED was observed before implementation:

```text
E   ModuleNotFoundError: No module named 'app.phone.verification'
```

After implementing the provider and typed settings, the focused suite passed:

```text
uv run pytest tests/unit/test_phone_verification.py tests/unit/test_phone_settings.py tests/unit/test_phone_summary.py -q
31 passed in 0.64s
```

The full repository suite passed:

```text
uv run pytest -q
756 passed, 12 skipped in 90.38s (0:01:30)
```

The skips are the repository's opt-in service, Playwright, live crawler, and real-call tests.

## Files

- `app/phone/verification.py`: strict typed context, candidate/result/arbitration/SMS schemas; independent Extractor, Verifier, Arbiter, and SMS provider methods; one retrying JSON transport with sanitized errors and metadata.
- `app/phone/summary.py`: typed `build_verification_context` snapshot and settings-based verification provider factory; existing summary rendering remains compatible.
- `app/settings/config.py`: pipeline/model/timeout/retry/ASR/lease/batch settings, effective model fallbacks, and production validation.
- `.env.example`: verification setting placeholders.
- `tests/unit/test_phone_verification.py`: recursive strictness, request isolation, fenced JSON, failure classification/retry, metadata, timeout privacy, and immutable metadata coverage.
- `tests/unit/test_phone_settings.py`: defaults, fallback, and enabled production validation coverage.

## Checks

```text
uv run ruff check app/phone/verification.py app/phone/summary.py app/settings/config.py tests/unit/test_phone_verification.py tests/unit/test_phone_settings.py
All checks passed!

uv run ruff format --check app/phone/verification.py app/phone/summary.py app/settings/config.py tests/unit/test_phone_verification.py tests/unit/test_phone_settings.py
5 files already formatted

uv run mypy app fixture_site
Success: no issues found in 116 source files
```

## Self-review

- Verifier user content is generated only from `VerificationContext`; extractor output is included only in Arbiter input.
- Every response model, including nested models, forbids additional properties.
- Transport errors expose stable reason codes only; exception text, prompts, phone numbers, SMS text, and API tokens are not returned.
- Owned HTTP clients disable redirects, use the existing preference header, and request temperature-zero strict JSON schemas.
- Model metadata records provider, selected model, positive latency, and attempts; failures retain metadata on `VerificationUnavailable.metadata`.
- Existing `PhoneSummaryProvider` remains for the current summary worker until the later atomic pipeline task switches callers to the multi-pass provider. The new summary context/factory provide the migration seam.

## Concerns for follow-up tasks

- Task 5 should use `build_verification_context` and independently call `extract`, `verify`, then `arbitrate`; this task intentionally does not persist reconciliation decisions.
- Task 6 should replace the legacy summary worker call with the multi-pass pipeline and persist `ModelCallMeta` per pass.
