from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.telemetry import external_call_metrics, record_external_call_attempts


async def test_external_call_metrics_group_dynamic_http_codes_and_transport_errors(
    sqlite_session_factory,
) -> None:
    now = datetime.now(UTC)
    async with sqlite_session_factory() as session:
        await record_external_call_attempts(
            session,
            attempts=[
                {
                    "occurred_at": now.isoformat(),
                    "provider": "google",
                    "model": "gemini-a",
                    "outcome": "error",
                    "http_status": 429,
                    "provider_error_code": "RESOURCE_EXHAUSTED",
                    "retryable": True,
                    "retry_after_seconds": 60,
                    "latency_ms": 11,
                },
                {
                    "occurred_at": (now + timedelta(milliseconds=1)).isoformat(),
                    "provider": "nvidia",
                    "model": "model-b",
                    "outcome": "timeout",
                    "http_status": None,
                    "exception_type": "ReadTimeout",
                    "retryable": True,
                    "latency_ms": 45000,
                },
                {
                    "occurred_at": (now + timedelta(milliseconds=2)).isoformat(),
                    "provider": "cerebras",
                    "model": "model-c",
                    "outcome": "success",
                    "http_status": 200,
                    "retryable": False,
                    "latency_ms": 8,
                },
            ],
            subsystem="matching",
            operation="llm_match",
            upstream_service="llmrouter",
            logical_request_id="req-recovered",
            recovered=True,
        )
        await record_external_call_attempts(
            session,
            attempts=[
                {
                    "occurred_at": (now + timedelta(seconds=1)).isoformat(),
                    "provider": "google",
                    "model": "gemini-d",
                    "outcome": "error",
                    "http_status": 503,
                    "provider_error_code": "UNAVAILABLE",
                    "retryable": True,
                    "latency_ms": 9,
                }
            ],
            subsystem="matching",
            operation="llm_match",
            upstream_service="llmrouter",
            logical_request_id="req-failed",
            recovered=False,
        )
        await session.commit()

        metrics = await external_call_metrics(
            session,
            now - timedelta(seconds=1),
            now + timedelta(seconds=2),
        )

    assert metrics["total_attempts"] == 4
    assert metrics["failed_attempts"] == 3
    assert metrics["logical_requests"] == 2
    assert metrics["affected_requests"] == 2
    assert metrics["recovered_requests"] == 1
    assert metrics["final_failed_requests"] == 1
    assert metrics["by_http_status"] == {"200": 1, "429": 1, "503": 1}
    assert metrics["http_errors_by_status"] == {"429": 1, "503": 1}
    assert metrics["http_errors_by_class"] == {"4xx": 1, "5xx": 1}
    assert metrics["by_exception_type"] == {"ReadTimeout": 1}
    assert metrics["errors_by_provider"] == {
        "google": 2,
        "nvidia": 1,
    }
    assert metrics["errors_by_subsystem"] == {"matching": 3}


async def test_external_call_metrics_accepts_unseen_http_status_without_schema_change(
    sqlite_session_factory,
) -> None:
    now = datetime.now(UTC)
    async with sqlite_session_factory() as session:
        await record_external_call_attempts(
            session,
            attempts=[
                {
                    "provider": "future-provider",
                    "model": "future-model",
                    "outcome": "error",
                    "http_status": 451,
                    "provider_error_code": "REGION_RESTRICTED",
                }
            ],
            subsystem="matching",
            operation="llm_match",
            upstream_service="future-router",
            logical_request_id="future-request",
        )
        await session.commit()
        metrics = await external_call_metrics(
            session,
            now - timedelta(seconds=1),
            now + timedelta(seconds=2),
        )

    assert metrics["http_errors_by_status"] == {"451": 1}
    assert metrics["http_errors_by_class"] == {"4xx": 1}
