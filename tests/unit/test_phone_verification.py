from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from app.phone.verification import (
    ArbitrationResult,
    ExtractionResult,
    FactCandidate,
    ModelCallMeta,
    PersistedFact,
    PostCallVerificationProvider,
    SmsComparisonResult,
    VerificationContext,
    VerificationResult,
    VerificationTurn,
    VerificationUnavailable,
)


def _context() -> VerificationContext:
    return VerificationContext(
        call_id="call-1",
        call_started_at=datetime(2026, 9, 7, 10, 0, tzinfo=UTC),
        timezone="Europe/Chisinau",
        transcript=[
            VerificationTurn(
                seq=1,
                turn_id=UUID("11111111-1111-1111-1111-111111111111"),
                speaker="employer",
                text="Собеседование завтра в 14:00.",
                asr_confidence=0.95,
                evidence_reference="evidence/1.wav",
            )
        ],
        company="Example",
        vacancy="Грузчик",
        application_status="sent",
    )


def _candidate() -> dict[str, object]:
    return {
        "field": "interview_time",
        "raw_expression": "в 14:00",
        "normalized_value": "14:00",
        "quote": "Собеседование завтра в 14:00.",
        "turn_seq": 1,
        "confidence": 0.9,
        "ambiguity": "",
    }


def _response(value: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]},
    )


def test_verification_models_are_recursive_strict() -> None:
    for model in (
        FactCandidate,
        ExtractionResult,
        VerificationResult,
        ArbitrationResult,
        VerificationTurn,
        VerificationContext,
        PersistedFact,
        SmsComparisonResult,
    ):
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        assert all(
            nested.get("additionalProperties") is False
            for nested in schema.get("$defs", {}).values()
            if isinstance(nested, dict) and "properties" in nested
        )

    assert "facts" in ExtractionResult.model_json_schema()["required"]
    assert "review_reasons" in ExtractionResult.model_json_schema()["required"]
    assert "facts" in VerificationResult.model_json_schema()["required"]
    assert "review_reasons" in VerificationResult.model_json_schema()["required"]
    assert "decisions" in ArbitrationResult.model_json_schema()["required"]
    assert "comparisons" in SmsComparisonResult.model_json_schema()["required"]


def test_strict_request_schemas_require_every_property_and_keep_defaults_nullable() -> None:
    provider = PostCallVerificationProvider(
        base_url="http://router",
        api_key="secret-token",
        model="model",
    )
    schemas = [
        provider._body("extractor", "system", "user", ExtractionResult)["response_format"][
            "json_schema"
        ]["schema"],
        provider._body("verifier", "system", "user", VerificationResult)["response_format"][
            "json_schema"
        ]["schema"],
        provider._body("arbiter", "system", "user", ArbitrationResult)["response_format"][
            "json_schema"
        ]["schema"],
        provider._body("sms", "system", "user", SmsComparisonResult)["response_format"][
            "json_schema"
        ]["schema"],
    ]

    def assert_all_properties_are_required(value: object) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                assert set(value.get("required", [])) == set(properties)
            for nested in value.values():
                assert_all_properties_are_required(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_all_properties_are_required(nested)

    for schema in schemas:
        assert_all_properties_are_required(schema)

    fact_schema = schemas[0]["$defs"]["FactCandidate"]
    assert {"type": "null"} in fact_schema["properties"]["ambiguity"]["anyOf"]
    candidate = FactCandidate.model_validate(_candidate() | {"ambiguity": None})
    assert candidate.ambiguity is None


def test_strict_requests_allow_reasoning_backends_to_finish_json() -> None:
    provider = PostCallVerificationProvider(
        base_url="http://router",
        api_key="secret-token",
        model="model",
    )

    body = provider._body("extractor", "system", "user", ExtractionResult)

    assert body["max_tokens"] == 1536


@pytest.mark.asyncio
async def test_verifier_request_contains_original_context_only() -> None:
    bodies: list[dict[str, object]] = []
    extraction = {
        "summary_text": "итог",
        "outcome_guess": "unclear",
        "facts": [_candidate() | {"normalized_value": "14:30"}],
        "review_reasons": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        bodies.append(json.loads(body))
        return _response(extraction if len(bodies) == 1 else {"facts": [], "review_reasons": []})

    provider = PostCallVerificationProvider(
        base_url="http://router",
        api_key="secret-token",
        model="model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    extracted, _ = await provider.extract(_context())
    await provider.verify(_context())

    assert bodies[0] != bodies[1]
    verifier_json = json.dumps(bodies[1], ensure_ascii=False)
    assert "extractor_result" not in verifier_json
    assert extracted.facts[0].normalized_value is not None
    assert extracted.facts[0].normalized_value not in verifier_json


@pytest.mark.asyncio
@pytest.mark.parametrize("pass_name", ["extractor", "verifier", "arbiter"])
async def test_sent_verification_contract_separates_expressions_from_context(
    pass_name: str,
) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if pass_name == "extractor":
            return _response(
                {"summary_text": "", "outcome_guess": "unclear", "facts": [], "review_reasons": []}
            )
        if pass_name == "arbiter":
            return _response({"decisions": []})
        return _response({"facts": [], "review_reasons": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = PostCallVerificationProvider(
            base_url="http://router", api_key="secret-token", model="model", client=client
        )
        if pass_name == "arbiter":
            await provider.arbitrate(
                _context(),
                ExtractionResult(
                    summary_text="", outcome_guess="unclear", facts=[], review_reasons=[]
                ),
                VerificationResult(facts=[], review_reasons=[]),
            )
        else:
            await getattr(provider, "extract" if pass_name == "extractor" else "verify")(_context())

    body = bodies[0]
    system = body["messages"][0]["content"]
    assert "raw_expression" in system
    assert "одного поля" in system
    assert "не переводите" in system.casefold()
    assert "падеж" in system.casefold()
    assert "не удаляйте" in system.casefold()  # uncertainty must survive short extraction
    schema = body["response_format"]["json_schema"]["schema"]
    definition = schema["$defs"]["ArbitrationItem" if pass_name == "arbiter" else "FactCandidate"]
    props = definition["properties"]
    if pass_name == "arbiter":
        assert "Russian" in props["accepted_value"]["description"]
        assert "bounded" in props["supporting_quote"]["description"]
    else:
        assert "shortest exact" in props["raw_expression"]["description"]
        assert "single field" in props["raw_expression"]["description"]
        assert "uncertainty" in props["raw_expression"]["description"]
        assert "Russian" in props["normalized_value"]["description"]
        assert "inflection" in props["normalized_value"]["description"]
        assert "bounded" in props["quote"]["description"]
        # Independent passes still receive only the untouched source context.
        assert json.loads(body["messages"][1]["content"]) == _context().model_dump(mode="json")


@pytest.mark.asyncio
async def test_provider_parses_fenced_json_and_records_metadata() -> None:
    payload = {"facts": [], "review_reasons": []}
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "```json\n" + json.dumps(payload) + "\n```"}}]},
    )
    provider = PostCallVerificationProvider(
        base_url="http://router",
        api_key="secret-token",
        model="model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)),
    )
    result, meta = await provider.verify(_context())
    assert result.facts == []
    assert meta.provider == "llmrouter"
    assert meta.latency_ms > 0
    assert meta.attempts == 1


@pytest.mark.asyncio
async def test_provider_retries_semantically_invalid_field_value() -> None:
    responses = iter(
        [
            {
                "facts": [
                    _candidate()
                    | {
                        "field": "meeting_url",
                        "raw_expression": "14.30",
                        "normalized_value": "14:30",
                    }
                ],
                "review_reasons": [],
            },
            {"facts": [_candidate()], "review_reasons": []},
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return _response(next(responses))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = PostCallVerificationProvider(
            base_url="http://router",
            api_key="secret-token",
            model="model",
            max_attempts=2,
            client=client,
        )
        result, meta = await provider.verify(_context())

    assert result.facts[0].field == "interview_time"
    assert meta.attempts == 2


@pytest.mark.asyncio
async def test_provider_sanitizes_transport_failures() -> None:
    provider = PostCallVerificationProvider(
        base_url="http://router",
        api_key="secret-token",
        model="model",
        max_attempts=1,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: (_ for _ in ()).throw(httpx.ReadTimeout("+37360111222 SMS secret-token"))
            )
        ),
    )
    with pytest.raises(VerificationUnavailable) as caught:
        await provider.verify(_context())
    assert str(caught.value) == "timeout"
    assert "+37360111222" not in str(caught.value)
    assert "secret-token" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    formatted = "".join(traceback.format_exception(caught.value))
    exposed = formatted + repr(caught.value.args) + repr(vars(caught.value))
    assert "+37360111222" not in exposed
    assert "secret-token" not in exposed
    assert "SMS" not in exposed


@pytest.mark.asyncio
async def test_provider_schema_failure_has_no_original_exception_context() -> None:
    provider = PostCallVerificationProvider(
        base_url="http://router",
        api_key="secret-token",
        model="model",
        max_attempts=1,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: _response(
                    {
                        "facts": [],
                        "review_reasons": [],
                        "invalid_output": "employer prompt with phone +37360111222",
                    }
                )
            )
        ),
    )
    with pytest.raises(VerificationUnavailable) as caught:
        await provider.verify(_context())
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    formatted = "".join(traceback.format_exception(caught.value))
    assert "invalid_output" not in formatted
    assert "+37360111222" not in formatted


@pytest.mark.asyncio
async def test_provider_rejects_redirects_even_when_client_allows_them() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(307, headers={"location": "https://evil.example/"})
        return _response({"facts": [], "review_reasons": []})

    provider = PostCallVerificationProvider(
        base_url="http://router",
        api_key="secret-token",
        model="model",
        max_attempts=1,
        client=httpx.AsyncClient(
            follow_redirects=True,
            transport=httpx.MockTransport(handler),
        ),
    )
    with pytest.raises(VerificationUnavailable, match="http_307"):
        await provider.verify(_context())
    assert calls == ["http://router/v1/chat/completions"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(429), "http_429"),
        (httpx.Response(500), "http_500"),
        (httpx.Response(200, content=b"not json"), "malformed_envelope"),
        (
            httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
            "empty_content",
        ),
        (_response({"facts": [], "review_reasons": [], "unexpected": True}), "schema_mismatch"),
    ],
)
async def test_provider_returns_sanitized_failure_codes(
    response: httpx.Response, reason: str
) -> None:
    provider = PostCallVerificationProvider(
        base_url="http://router",
        api_key="secret-token",
        model="model",
        max_attempts=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)),
    )
    with pytest.raises(VerificationUnavailable) as caught:
        await provider.verify(_context())
    assert str(caught.value) == reason
    assert caught.value.metadata is not None
    assert caught.value.metadata.attempts == 1


@pytest.mark.asyncio
async def test_provider_retries_rate_limit_then_returns_result() -> None:
    responses = iter([httpx.Response(429), _response({"facts": [], "review_reasons": []})])
    provider = PostCallVerificationProvider(
        base_url="http://router",
        api_key="secret-token",
        model="model",
        max_attempts=3,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))),
    )
    _, metadata = await provider.verify(_context())
    assert metadata.attempts == 2


@pytest.mark.asyncio
async def test_provider_uses_bounded_exponential_backoff_for_transient_failures() -> None:
    responses = iter(
        [httpx.Response(503), httpx.Response(429), _response({"facts": [], "review_reasons": []})]
    )
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    provider = PostCallVerificationProvider(
        base_url="http://router",
        api_key="secret-token",
        model="model",
        max_attempts=3,
        sleeper=sleeper,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))),
    )
    _, metadata = await provider.verify(_context())
    assert metadata.attempts == 3
    assert delays == [0.25, 0.5]


def test_model_metadata_is_immutable() -> None:
    metadata = ModelCallMeta(provider="llmrouter", model="model", latency_ms=1, attempts=1)
    with pytest.raises((AttributeError, TypeError)):
        metadata.attempts = 2  # type: ignore[misc]
