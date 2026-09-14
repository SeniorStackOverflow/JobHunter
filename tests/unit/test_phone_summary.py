import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.models.entities import CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnSpeaker,
)
from app.phone import summary as summary_module
from app.phone.summary import (
    CallSummary,
    CallSummaryContext,
    PhoneSummaryProvider,
    PhoneSummaryUnavailable,
)
from app.phone.verification import (
    ArbitrationResult,
    ExtractionResult,
    ModelCallMeta,
    VerificationResult,
)

_CTX = CallSummaryContext(
    transcript=[
        ("assistant", "Здравствуйте"),
        ("employer", "Звоню по вакансии грузчика, в четверг в 14"),
    ],
    company="Example SRL",
    vacancy="Грузчик",
    application_status="sent",
    confirmed_facts={},
)


def _ok_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]},
    )


def test_call_summary_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CallSummary.model_validate({"summary_text": "Итог", "unexpected": True})


def test_summary_body_includes_confirmed_profile_facts() -> None:
    provider = PhoneSummaryProvider(base_url="http://r", api_key="k", model="m")
    body = provider._body(
        CallSummaryContext(
            transcript=[("employer", "Звоню по вакансии")],
            confirmed_facts={"confirmed_facts": [{"field": "name", "value": "Андрей"}]},
        )
    )
    content = body["messages"][1]["content"]
    assert "Подтверждённые данные кандидата" in content
    assert "Андрей" in content


@pytest.mark.asyncio
async def test_summarize_parses_valid_json():
    payload = {
        "summary_text": "Работодатель предложил собеседование в четверг в 14:00.",
        "mentioned_vacancy": "Грузчик",
        "proposed_datetime_text": "в четверг в 14",
        "proposed_address_text": "",
        "contact_person_text": "",
        "outcome_guess": "interview_proposed",
        "needs_review": False,
    }
    seen = {}

    def handler(request):
        seen["prefer"] = request.headers.get("X-LLMRouter-Prefer")
        seen["url"] = str(request.url)
        return _ok_response(payload)

    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await p.summarize(_CTX)
    assert result.outcome_guess == "interview_proposed"
    assert result.proposed_datetime_text == "в четверг в 14"
    assert seen["prefer"] == "quality"
    assert seen["url"].endswith("/v1/chat/completions")
    assert p.last_latency_ms is not None
    assert p.last_latency_ms >= 0


@pytest.mark.asyncio
async def test_finalize_persists_provider_latency_metadata(
    sqlite_session_factory, tmp_path, monkeypatch
) -> None:
    settings = summary_module.Settings(
        _env_file=None,
        phone_evidence_dir=tmp_path,
        phone_summary_llm_enabled=True,
        phone_summary_llm_model="summary-model",
        phone_summary_llm_api_key=SecretStr("router-key"),
        phone_summary_batch=10,
        telegram_enabled=False,
    )
    now = datetime.now(UTC)
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="default", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address="+37360111222",
            remote_raw="+37360111222",
            phonegate_event_id_start=1,
            started_at=now,
            ended_at=now,
            outcome=CommunicationOutcome.COMPLETED,
            auto_answered=True,
            summary_state=PhoneSummaryState.PENDING,
        )
        db.add(call)
        await db.flush()
        db.add(
            CommunicationTurn(
                session_id=call.id,
                phonegate_transcript_id=1,
                seq=1,
                speaker=TurnSpeaker.EMPLOYER,
                text="Звоню по вакансии",
                occurred_at=now,
            )
        )
        await db.commit()
        session_id = call.id

    class Provider:
        async def extract(self, context):
            return ExtractionResult(
                summary_text="Итог звонка",
                outcome_guess="info_request",
                facts=[],
                review_reasons=[],
            ), ModelCallMeta("llmrouter", "summary-model", 12, 1)

        async def verify(self, context):
            return VerificationResult(facts=[], review_reasons=[]), ModelCallMeta(
                "llmrouter", "summary-model", 9, 1
            )

        async def arbitrate(self, context, extracted, verified):
            return ArbitrationResult(decisions=[]), ModelCallMeta(
                "llmrouter", "summary-model", 7, 1
            )

    monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
    monkeypatch.setattr(summary_module, "get_settings", lambda: settings)
    monkeypatch.setattr(summary_module, "_build_verification_provider", lambda _: Provider())

    await summary_module.finalize_pending_calls()

    async with sqlite_session_factory() as db:
        call = await db.get(CommunicationSession, session_id)
    assert call is not None
    assert call.summary["model_meta"]["latency_ms"] is not None
    assert isinstance(call.summary["model_meta"]["latency_ms"], int)


@pytest.mark.asyncio
async def test_summarize_rejects_non_json():
    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"finish_reason": "stop", "message": {"content": "sorry I cannot"}}
                        ]
                    },
                )
            )
        ),
    )
    with pytest.raises(PhoneSummaryUnavailable):
        await p.summarize(_CTX)


@pytest.mark.asyncio
async def test_summarize_maps_5xx_and_timeout():
    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
    )
    with pytest.raises(PhoneSummaryUnavailable):
        await p.summarize(_CTX)


@pytest.mark.asyncio
async def test_summarize_strips_markdown_fence():
    fenced = "```json\n" + json.dumps({"summary_text": "ок"}) + "\n```"
    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={"choices": [{"finish_reason": "stop", "message": {"content": fenced}}]},
                )
            )
        ),
    )
    assert (await p.summarize(_CTX)).summary_text == "ок"


@pytest.mark.asyncio
async def test_no_employer_turns_prompt_rejection_is_not_manual_review(
    sqlite_session_factory, tmp_path, monkeypatch
) -> None:
    settings = summary_module.Settings(
        _env_file=None,
        phone_evidence_dir=tmp_path,
        phone_summary_llm_enabled=True,
        phone_summary_llm_model="summary-model",
        phone_summary_llm_api_key=SecretStr("router-key"),
        telegram_enabled=False,
    )
    now = datetime.now(UTC)
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="default", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address="+37360111222",
            remote_raw="+37360111222",
            started_at=now,
            answered_at=now,
            ended_at=now,
            outcome=CommunicationOutcome.COMPLETED,
            auto_answered=True,
            summary_state=PhoneSummaryState.PENDING,
            script_stage="remote_ended",
            diagnostics={"remote_end_phase": "intro_tts"},
        )
        db.add(call)
        await db.commit()
        call_id = call.id

    monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
    monkeypatch.setattr(summary_module, "get_settings", lambda: settings)

    result = await summary_module.finalize_pending_calls()
    assert result["picked"] == 1
    assert result["skipped"] == 1
    async with sqlite_session_factory() as db:
        call = await db.get(CommunicationSession, call_id)
    assert call is not None
    assert call.summary_state is PhoneSummaryState.SKIPPED
    assert call.verification_status is PhoneVerificationStatus.NOT_APPLICABLE
    assert call.needs_review is False
    assert call.summary["post_call_disposition"] == "probable_prompt_rejection"


@pytest.mark.asyncio
async def test_no_employer_turns_unexplained_stays_needs_review(
    sqlite_session_factory, tmp_path, monkeypatch
) -> None:
    settings = summary_module.Settings(
        _env_file=None,
        phone_evidence_dir=tmp_path,
        phone_summary_llm_enabled=True,
        phone_summary_llm_model="summary-model",
        phone_summary_llm_api_key=SecretStr("router-key"),
        telegram_enabled=False,
    )
    now = datetime.now(UTC)
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="default", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address="+37360111222",
            remote_raw="+37360111222",
            started_at=now,
            answered_at=now,
            ended_at=now,
            outcome=CommunicationOutcome.COMPLETED,
            auto_answered=True,
            summary_state=PhoneSummaryState.PENDING,
            diagnostics={
                "phonegate_end_reason": "remote_or_network_hangup",
                "peer_hangup_ms_after_last_tts": 9000,
                "call_disposition": "remote_hangup_no_employer_transcript",
            },
        )
        db.add(call)
        await db.commit()
        call_id = call.id

    monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
    monkeypatch.setattr(summary_module, "get_settings", lambda: settings)

    result = await summary_module.finalize_pending_calls()
    assert result["picked"] == 1
    assert result["skipped"] == 1
    async with sqlite_session_factory() as db:
        call = await db.get(CommunicationSession, call_id)
    assert call is not None
    assert call.summary_state is PhoneSummaryState.SKIPPED
    assert call.verification_status is PhoneVerificationStatus.NEEDS_REVIEW
    assert call.needs_review is True
    assert call.summary["post_call_disposition"] == "remote_hangup_no_employer_transcript"
