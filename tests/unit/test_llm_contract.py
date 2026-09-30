from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.base import Base
from app.matching import LLMProviderUnavailable, LLMRouterProvider, MatchingService, MatchResult
from app.matching.prefilter import DeterministicPrefilter
from app.matching.schemas import MATCH_RESULT_OUTBOUND_SCHEMA
from app.matching.service import reconcile_match_result
from app.models.entities import (
    CanonicalJob,
    ExternalCallEvent,
    JobSource,
    MatchEvaluation,
    Resume,
)
from app.models.enums import JobStatus, MatchDecision, SourceHealth
from app.settings import Settings
from app.telemetry import external_call_metrics, record_external_call_attempts
from tests.unit.test_matching import (
    make_job,
    make_preference,
    make_profile,
    make_request,
    make_result,
)


def _walk_objects(node: Any):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for value in node.values():
            yield from _walk_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_objects(value)


def _completion(content: str, *, finish_reason: str = "stop", trace: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}]
    }
    if trace is not None:
        payload["_llmrouter"] = {"attempts": trace}
    return payload


def _provider(client: httpx.AsyncClient, *, attempts: int = 2) -> LLMRouterProvider:
    return LLMRouterProvider(
        model="jobhunter",
        api_key="router-key",
        base_url="http://router.example.test",
        client=client,
        max_attempts=attempts,
        retry_delay_seconds=0,
    )


def test_outbound_schema_is_strict_and_portable() -> None:
    serialized = json.dumps(MATCH_RESULT_OUTBOUND_SCHEMA)
    assert "$ref" not in serialized and "$defs" not in serialized
    assert '"default"' not in serialized
    objects = list(_walk_objects(MATCH_RESULT_OUTBOUND_SCHEMA))
    assert objects
    for node in objects:
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])
    properties = MATCH_RESULT_OUTBOUND_SCHEMA["properties"]
    assert isinstance(properties, dict)
    # Fields with local default factories are still required outbound.
    assert {"soft_mismatches", "optional_requirements_missing"} <= set(
        MATCH_RESULT_OUTBOUND_SCHEMA["required"]  # type: ignore[arg-type]
    )
    # Literal/Enum constraints are kept, not weakened.
    assert properties["soft_mismatches"]["items"]["enum"] == [
        "skills",
        "preferred_experience",
        "resume_relevance",
        "optional_requirement",
    ]
    assert properties["decision"]["enum"] == ["auto_apply", "prepare_for_review", "skip", "block"]
    # The local model still enforces the dropped bounds.
    with pytest.raises(ValueError):
        MatchResult.model_validate({**make_result().model_dump(mode="json"), "overall_fit": 101})


async def test_outgoing_body_carries_the_strict_contract() -> None:
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, request=request, json=_completion(make_result().model_dump_json())
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result, trace = await _provider(client).evaluate_with_trace(make_request())

    assert result == make_result()
    assert trace.schema_valid is True and trace.app_attempts == 1
    json_schema = bodies[0]["response_format"]["json_schema"]
    assert json_schema["strict"] is True
    assert json_schema["schema"] == MATCH_RESULT_OUTBOUND_SCHEMA
    assert bodies[0]["max_tokens"] == LLMRouterProvider.BASE_MAX_TOKENS


async def test_literal_error_retries_once_with_path_feedback_and_no_raw_value() -> None:
    invalid = make_result().model_dump(mode="json")
    invalid["soft_mismatches"] = ["experience"]
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        content = json.dumps(invalid) if len(bodies) == 1 else make_result().model_dump_json()
        return httpx.Response(200, request=request, json=_completion(content))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result, trace = await _provider(client).evaluate_with_trace(make_request())

    assert result == make_result()
    assert trace.schema_valid is True
    assert trace.repairs == ("schema_feedback",)
    assert len(bodies) == 2
    assert len(bodies[0]["messages"]) == 2
    note = bodies[1]["messages"][-1]["content"]
    assert "schema_validation:literal_error" in note
    assert "soft_mismatches.0" in note
    assert "experience" not in note


async def test_invalid_output_after_bounded_retries_is_review_only() -> None:
    invalid = make_result(decision=MatchDecision.AUTO_APPLY, overall_fit=99).model_dump(mode="json")
    invalid["soft_mismatches"] = ["experience"]
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request, json=_completion(json.dumps(invalid)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result, trace = await _provider(client, attempts=2).evaluate_with_trace(make_request())

    assert calls == 2
    assert trace.schema_valid is False
    assert trace.failure_code == "schema_validation:literal_error"
    assert trace.failure_path == "soft_mismatches.0"
    assert result.overall_fit == 0
    assert result.decision is MatchDecision.PREPARE_FOR_REVIEW
    assert result.risks == ["llm_provider_failure:llmrouter:schema_validation:literal_error"]

    job, profile, preference = make_job(), make_profile(), make_preference()
    deterministic = DeterministicPrefilter().evaluate(job, preference, profile, resume_fit=90)
    reconciled = reconcile_match_result(deterministic, result, minimum_auto_send_score=0)
    assert reconciled.decision is not MatchDecision.AUTO_APPLY


async def test_length_truncation_raises_budget_once_then_stops() -> None:
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, request=request, json=_completion('{"resume_fit": 9', finish_reason="length")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result, trace = await _provider(client, attempts=5).evaluate_with_trace(make_request())

    assert [body["max_tokens"] for body in bodies] == [
        LLMRouterProvider.BASE_MAX_TOKENS,
        LLMRouterProvider.MAX_TOKENS_CEILING,
    ]
    assert trace.repairs == ("length_budget",)
    assert trace.failure_code == "finish_reason:length"
    assert result.decision is MatchDecision.PREPARE_FOR_REVIEW
    assert result.risks == ["llm_provider_failure:llmrouter:finish_reason:length"]


async def test_deterministic_router_400_is_not_repeated() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400, request=request, json={"error": {"type": "invalid_request", "message": "bad"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMProviderUnavailable):
            await _provider(client, attempts=4).evaluate_with_trace(make_request())
    assert calls == 1


async def test_router_synthetic_502_keeps_upstream_status_distinct_from_real_502() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).isoformat()
    attempts = [
        {
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "outcome": "error",
            "http_status": 502,
            "upstream_status": 400,
            "router_synthetic": True,
            "failure_class": "structured_output_rejected",
            "occurred_at": now,
        },
        {
            "provider": "google",
            "model": "gemini",
            "outcome": "error",
            "http_status": 502,
            "upstream_status": 200,
            "router_synthetic": True,
            "failure_class": "truncated",
            "occurred_at": now,
        },
        {
            "provider": "nvidia",
            "model": "m",
            "outcome": "error",
            "http_status": 502,
            "occurred_at": now,
        },
        {
            "provider": "cerebras",
            "model": "m",
            "outcome": "success",
            "http_status": 200,
            "occurred_at": now,
        },
    ]
    try:
        async with factory() as session:
            await record_external_call_attempts(
                session,
                attempts=attempts,
                subsystem="matching",
                operation="llm_match",
                upstream_service="llmrouter",
                logical_request_id="request-1",
                recovered=True,
                metadata={"schema_validated": False},
            )
            await session.commit()
            rows = (
                await session.scalars(
                    select(ExternalCallEvent).order_by(ExternalCallEvent.attempt_no)
                )
            ).all()
            assert rows[0].event_metadata["upstream_status"] == 400
            assert rows[0].event_metadata["failure_class"] == "structured_output_rejected"
            assert rows[1].event_metadata["upstream_status"] == 200
            assert "router_synthetic" not in rows[2].event_metadata
            metrics = await external_call_metrics(
                session,
                datetime(2000, 1, 1, tzinfo=UTC),
                datetime(2100, 1, 1, tzinfo=UTC),
            )
    finally:
        await engine.dispose()

    assert metrics["http_errors_by_status"] == {"502": 3}
    assert metrics["router_synthetic_failures"] == 2
    assert metrics["router_synthetic_by_status"] == {"502<-200": 1, "502<-400": 1}
    assert metrics["transport_recovered_requests"] == 1
    assert metrics["schema_checked_requests"] == 1
    assert metrics["schema_invalid_requests"] == 1
    assert metrics["schema_validated_requests"] == 0


async def test_analyze_persists_logical_request_and_schema_outcome() -> None:
    invalid = make_result().model_dump(mode="json")
    invalid["soft_mismatches"] = ["experience"]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=_completion(
                json.dumps(invalid),
                trace=[
                    {
                        "provider": "cloudflare",
                        "model": "granite",
                        "outcome": "success",
                        "http_status": 200,
                    }
                ],
            ),
        )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
            factory() as session,
        ):
            source = JobSource(
                name="Fixture",
                base_url="https://jobs.example.test",
                adapter_type="fixture_source",
                configuration={},
                enabled=True,
                health_status=SourceHealth.HEALTHY,
            )
            canonical = CanonicalJob(
                normalized_company="example employer",
                normalized_title="python developer",
                normalized_location="chisinau",
                canonical_fingerprint="a" * 64,
                status=JobStatus.ACTIVE,
            )
            session.add_all([source, canonical])
            await session.flush()
            job = make_job(
                source_id=source.id,
                canonical_job_id=canonical.id,
                title="Python developer",
                category="engineering",
                categories_seen=["engineering"],
            )
            profile = make_profile()
            profile.id = uuid4()
            preference = make_preference(profile_id=profile.id, allowed_categories=["engineering"])
            resume = Resume(
                profile_id=profile.id,
                name="Engineering CV",
                category="engineering",
                storage_key="cv.pdf",
                original_filename="cv.pdf",
                mime_type="application/pdf",
                sha256="7" * 64,
                active=True,
                verified=True,
                is_default=True,
            )
            session.add_all([job, profile, preference, resume])
            await session.flush()
            service = MatchingService(Settings(environment="test"), _provider(client, attempts=2))
            evaluation = await service.analyze(session, job.id)
            events = (await session.scalars(select(ExternalCallEvent))).all()

            assert evaluation.llm_outcome == "invalid_output"
            assert evaluation.llm_failure_code == "schema_validation:literal_error"
            assert evaluation.llm_failure_path == "soft_mismatches.0"
            assert evaluation.llm_attempts == 2
            assert evaluation.llm_logical_request_id
            assert evaluation.decision is not MatchDecision.AUTO_APPLY
            assert {event.logical_request_id for event in events} == {
                evaluation.llm_logical_request_id
            }
            assert all(event.event_metadata["schema_validated"] is False for event in events)
            assert {event.event_metadata["match_evaluation_id"] for event in events} == {
                str(evaluation.id)
            }
            stored = await session.get(MatchEvaluation, evaluation.id)
            assert stored is not None and stored.llm_outcome == "invalid_output"
    finally:
        await engine.dispose()
