from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.database.base import Base
from app.database.session import make_session_factory
from app.models.entities import CallFact, CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import (
    CallFactState,
    CommunicationOutcome,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnSpeaker,
)
from app.phone.client import PhoneGateClient, PhoneGateUnavailable
from app.phone.correlation import CorrelationResult
from app.phone.orchestrator import CallOrchestrator
from app.phone.sessions import SessionStore
from app.phone.sms import (
    MemorySmsSyncMarker,
    ingest_phonegate_sms,
    reconcile_pending_sms,
)
from app.phone.summary import finalize_pending_calls
from app.phone.telegram import deliver_pending_phone_notifications
from app.phone.verification import (
    ModelCallMeta,
    PostCallVerificationProvider,
    SmsComparisonResult,
    SmsFieldComparison,
)
from app.settings.config import Settings
from tests.fixtures.fake_phonegate import FakePhoneGate


@pytest_asyncio.fixture
async def verification_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'phone-verification.db'}",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=30,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as db:
        db.add(UserProfile(name="Andrei", is_default=True, phone="+37360000000"))
        await db.commit()
    yield factory
    await engine.dispose()


def _settings(evidence_dir: Path, *, telegram: bool = True) -> Settings:
    return Settings.model_construct(
        phone_summary_llm_enabled=True,
        phone_summary_llm_base_url="http://llmrouter",
        phone_summary_llm_api_key=SecretStr("router-test-key"),
        phone_summary_llm_model="verification-model",
        phone_evidence_dir=evidence_dir,
        phone_evidence_min_chars=10,
        phone_evidence_seconds=8,
        phone_evidence_max_clips_per_call=3,
        phone_auto_answer_enabled=True,
        phone_answer_connect_timeout_seconds=2.0,
        phone_post_connect_wait_seconds=0.0,
        phone_speak_fence_timeout_seconds=2.0,
        phone_tx_idle_timeout_seconds=2.0,
        phone_inter_block_listen_seconds=0.001,
        phone_listen_silence_timeout_seconds=0.03,
        phone_call_hard_cap_seconds=3.0,
        phone_orchestrator_poll_seconds=0.001,
        phone_sms_sync_poll_delay_seconds=0.0,
        phone_sms_batch=150,
        phone_caller_region="MD",
        telegram_enabled=telegram,
        telegram_bot_token=SecretStr("bot-test-token") if telegram else None,
        telegram_chat_id="chat-test" if telegram else None,
        phone_telegram_retry_base_seconds=1,
        phone_telegram_retry_max_seconds=2,
    )


def _candidate(
    field: str, raw: str, normalized: str, quote: str, *, turn_seq: int = 99
) -> dict[str, object]:
    return {
        "field": field,
        "raw_expression": raw,
        "normalized_value": normalized,
        "quote": quote,
        "turn_seq": turn_seq,
        "confidence": 0.98,
        "ambiguity": "",
    }


def _model_response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
    )


async def _wait_for_stage(
    factory: async_sessionmaker[AsyncSession], session_id: UUID, stage: str
) -> None:
    for _ in range(400):
        await asyncio.sleep(0.005)
        async with factory() as db:
            call = await db.get(CommunicationSession, session_id)
        if call is not None and call.script_stage == stage:
            return
    pytest.fail(f"call did not reach {stage}")


async def _run_call(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    fake: FakePhoneGate,
    *,
    fail_audio: bool = False,
) -> UUID:
    async with factory() as db:
        profile = await db.scalar(select(UserProfile).where(UserProfile.is_default.is_(True)))
        assert profile is not None
        call = await SessionStore().open(
            db,
            remote_raw="+37360000000",
            remote_address="+37360000000",
            event_id=1,
            correlation=CorrelationResult(profile.id, None, None, None, None),
            opened_at=datetime(2026, 9, 7, 10, 0, tzinfo=UTC),
        )
        await db.commit()
        session_id = call.id

    fake.set_call_audio(b"RIFF....WAVEdata")
    if fail_audio:
        fake.fail_next_audio()
    fake.ring("+37360000000")
    async with PhoneGateClient(
        base_url="http://phonegate", token="test", transport=fake.transport()
    ) as client:
        task = asyncio.create_task(
            CallOrchestrator(client=client, session_factory=factory, settings=settings).run(
                session_id
            )
        )
        await _wait_for_stage(factory, session_id, "listening")
        transcript_id = fake.transcript(
            speaker="rx",
            text="Собеседование 12 сентября в 14:30, улица Индепенденцей 10",
        )
        assert await task == "greeting_completed"
    async with factory() as db:
        call = await db.get(CommunicationSession, session_id)
        assert call is not None
        db.add(
            CommunicationTurn(
                session_id=session_id,
                phonegate_transcript_id=transcript_id,
                seq=99,
                speaker=TurnSpeaker.EMPLOYER,
                text="Собеседование 12 сентября в 14:30, улица Индепенденцей 10",
                raw_text="Собеседование 12 сентября в 14:30, улица Индепенденцей 10",
                asr_confidence=0.98,
                occurred_at=datetime(2026, 9, 7, 10, 0, tzinfo=UTC),
            )
        )
        await SessionStore().close(
            db,
            call,
            outcome=CommunicationOutcome.COMPLETED,
            ended_at=datetime(2026, 9, 7, 10, 1, tzinfo=UTC),
        )
        await db.commit()
    return session_id


def _strict_pass_payloads(quote: str) -> list[dict[str, object]]:
    facts = [
        _candidate("interview_date", "12 сентября", "2026-09-12", quote),
        _candidate("interview_time", "14:30", "14:30", quote),
        _candidate("address", "улица Индепенденцей 10", "улица Индепенденцей 10", quote),
    ]
    return [
        {
            "summary_text": "Работодатель предложил собеседование.",
            "outcome_guess": "interview_proposed",
            "facts": facts,
            "review_reasons": [],
        },
        {"facts": facts, "review_reasons": []},
        {
            "decisions": [
                {
                    "field": item["field"],
                    "accepted_value": item["normalized_value"],
                    "supporting_quote": quote,
                    "accepted": True,
                    "reason": "direct evidence",
                }
                for item in facts
            ]
        },
    ]


async def _finalize_call(
    factory: async_sessionmaker[AsyncSession],
    session_id: UUID,
    settings: Settings,
    quote: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    transient_provider_error: bool = False,
) -> None:
    response_values: list[object] = _strict_pass_payloads(quote)
    if transient_provider_error:
        response_values.insert(0, httpx.Response(503))
    responses = iter(response_values)

    def handler(_request: httpx.Request) -> httpx.Response:
        response = next(responses)
        return response if isinstance(response, httpx.Response) else _model_response(response)

    async def no_sleep(_seconds: float) -> None:
        return None

    provider = PostCallVerificationProvider(
        base_url="http://llmrouter",
        api_key="router-test-key",
        model="verification-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleeper=no_sleep,
    )
    import app.phone.summary as summary_module

    monkeypatch.setattr(summary_module, "get_settings", lambda: settings)
    monkeypatch.setattr(summary_module, "_build_verification_provider", lambda _: provider)
    monkeypatch.setattr("app.database.session.async_session_factory", factory)
    result = await finalize_pending_calls()
    assert result == {"picked": 1, "done": 1, "failed": 0, "skipped": 0}


class _SmsProvider:
    def __init__(self, comparisons: list[SmsFieldComparison]) -> None:
        self.comparisons = comparisons

    async def compare_sms(
        self, _context: object, _text: str, _facts: object
    ) -> tuple[SmsComparisonResult, ModelCallMeta]:
        return SmsComparisonResult(comparisons=self.comparisons), ModelCallMeta(
            "llmrouter", "sms-model", 1, 1
        )


@pytest.mark.asyncio
async def test_call_evidence_three_passes_and_telegram_state(
    verification_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "evidence")
    fake = FakePhoneGate()
    session_id = await _run_call(verification_factory, settings, fake)
    async with verification_factory() as db:
        seeded_call = await db.get(CommunicationSession, session_id)
    assert seeded_call is not None
    assert seeded_call.auto_answered is True
    assert seeded_call.summary_state is PhoneSummaryState.PENDING
    async with verification_factory() as db:
        seeded_turns = list(
            (
                await db.scalars(
                    select(CommunicationTurn).where(
                        CommunicationTurn.session_id == session_id,
                        CommunicationTurn.speaker == TurnSpeaker.EMPLOYER,
                    )
                )
            ).all()
        )
    assert seeded_turns
    quote = "Собеседование 12 сентября в 14:30, улица Индепенденцей 10"
    responses = iter(_strict_pass_payloads(quote))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _model_response(next(responses))

    provider = PostCallVerificationProvider(
        base_url="http://llmrouter",
        api_key="router-test-key",
        model="verification-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    import app.phone.summary as summary_module

    monkeypatch.setattr(summary_module, "get_settings", lambda: settings)
    monkeypatch.setattr(summary_module, "_build_verification_provider", lambda _: provider)
    monkeypatch.setattr("app.database.session.async_session_factory", verification_factory)

    result = await finalize_pending_calls()
    assert result == {"picked": 1, "done": 1, "failed": 0, "skipped": 0}
    assert len(requests) == 3

    async with verification_factory() as db:
        call = await db.get(CommunicationSession, session_id)
        facts = list(
            (
                await db.scalars(
                    select(CallFact)
                    .where(CallFact.session_id == session_id)
                    .order_by(CallFact.field)
                )
            ).all()
        )
        turn = await db.scalar(
            select(CommunicationTurn).where(
                CommunicationTurn.session_id == session_id,
                CommunicationTurn.speaker == TurnSpeaker.EMPLOYER,
            )
        )
    assert call is not None
    assert call.summary_state is PhoneSummaryState.DONE
    assert call.summary["verification"], call.summary
    assert call.verification_status is PhoneVerificationStatus.HIGH_CONFIDENCE, call.summary
    assert {fact.field for fact in facts} == {"interview_date", "interview_time", "address"}
    assert all(fact.state is CallFactState.CANDIDATE for fact in facts)
    assert turn is not None and turn.audio_evidence_path is not None
    assert (
        settings.phone_evidence_dir / turn.audio_evidence_path
    ).read_bytes() == b"RIFF....WAVEdata"
    assert call.summary["telegram"]["state"] == "pending"

    sent: list[str] = []
    import app.phone.telegram as telegram_module

    async def fake_send(**kwargs: Any) -> Any:
        sent.append(str(kwargs["text"]))
        return type("Result", (), {"message_id": 42})()

    monkeypatch.setattr(telegram_module, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_module, "send_telegram_message", fake_send)
    delivery = await deliver_pending_phone_notifications()
    assert delivery["sent"] == 1
    assert sent and "+37360000000" not in sent[0]
    async with verification_factory() as db:
        call = await db.get(CommunicationSession, session_id)
    assert call is not None and call.summary["telegram"]["state"] == "sent"


def _sms_comparisons(
    *, date_expression: str, relation: str = "matches"
) -> list[SmsFieldComparison]:
    return [
        SmsFieldComparison(
            field="interview_date",
            relation=relation,  # type: ignore[arg-type]
            sms_expression=date_expression,
            call_expression="12 сентября",
            reason="same date" if relation == "matches" else "different date",
        ),
        SmsFieldComparison(
            field="interview_time",
            relation="matches",
            sms_expression="14:30",
            call_expression="14:30",
            reason="same time",
        ),
        SmsFieldComparison(
            field="address",
            relation="matches",
            sms_expression="улица Индепенденцей 10",
            call_expression="улица Индепенденцей 10",
            reason="same place",
        ),
    ]


@pytest.mark.asyncio
async def test_matching_sms_confirms_facts_and_reimport_is_idempotent(
    verification_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "matching", telegram=False)
    fake = FakePhoneGate()
    session_id = await _run_call(verification_factory, settings, fake)
    quote = "Собеседование 12 сентября в 14:30, улица Индепенденцей 10"
    await _finalize_call(verification_factory, session_id, settings, quote, monkeypatch)
    async with verification_factory() as db:
        call = await db.get(CommunicationSession, session_id)
        assert call is not None and call.ended_at is not None
        ended_at = call.ended_at
        ended_at_utc = ended_at.replace(tzinfo=UTC)
        revision_before = call.verification_revision
    fake.add_sms(
        id="sms-match-1",
        address="+37360000000",
        text="Подтверждаем 12 сентября в 14:30, улица Индепенденцей 10",
        timestamp=int(ended_at_utc.timestamp() * 1000),
    )
    marker = MemorySmsSyncMarker()
    async with PhoneGateClient(
        base_url="http://phonegate", token="test", transport=fake.transport()
    ) as client:
        imported = await ingest_phonegate_sms(
            client=client,
            session_factory=verification_factory,
            settings=settings,
            sync_marker=marker,
            now_factory=lambda: datetime.now(UTC),
            boot_id="matching-e2e",
        )
    assert imported["imported"] == 1 and imported["correlated"] == 1, imported
    provider = _SmsProvider(_sms_comparisons(date_expression="12 сентября"))
    result = await reconcile_pending_sms(
        session_factory=verification_factory, provider=provider, settings=settings
    )
    assert result == {"picked": 1, "done": 1, "failed": 0, "skipped": 0}
    async with verification_factory() as db:
        call = await db.get(CommunicationSession, session_id)
        facts = list(
            (await db.scalars(select(CallFact).where(CallFact.session_id == session_id))).all()
        )
        assert call is not None
        revision_after = call.verification_revision
    assert call.verification_status is PhoneVerificationStatus.CONFIRMED
    assert all(fact.state is CallFactState.CONFIRMED for fact in facts)
    assert all(fact.confirmed_by_turn_id is not None for fact in facts)
    assert revision_after == revision_before + 2

    async with PhoneGateClient(
        base_url="http://phonegate", token="test", transport=fake.transport()
    ) as client:
        repeated = await ingest_phonegate_sms(
            client=client,
            session_factory=verification_factory,
            settings=settings,
            sync_marker=marker,
            now_factory=lambda: datetime.now(UTC),
            boot_id="matching-e2e",
        )
    assert repeated["duplicates"] == 1
    assert (
        await reconcile_pending_sms(
            session_factory=verification_factory, provider=provider, settings=settings
        )
    )["picked"] == 0
    async with verification_factory() as db:
        call = await db.get(CommunicationSession, session_id)
    assert call is not None and call.verification_revision == revision_after


@pytest.mark.asyncio
async def test_conflicting_sms_preserves_no_confirmation_and_needs_review(
    verification_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "conflict", telegram=False)
    fake = FakePhoneGate()
    session_id = await _run_call(verification_factory, settings, fake)
    quote = "Собеседование 12 сентября в 14:30, улица Индепенденцей 10"
    await _finalize_call(verification_factory, session_id, settings, quote, monkeypatch)
    async with verification_factory() as db:
        call = await db.get(CommunicationSession, session_id)
        assert call is not None and call.ended_at is not None
        ended_at = call.ended_at
        ended_at_utc = ended_at.replace(tzinfo=UTC)
    fake.add_sms(
        id="sms-conflict-1",
        address="+37360000000",
        text="Подтверждаем 13 сентября в 14:30, улица Индепенденцей 10",
        timestamp=int(ended_at_utc.timestamp() * 1000),
    )
    async with PhoneGateClient(
        base_url="http://phonegate", token="test", transport=fake.transport()
    ) as client:
        imported = await ingest_phonegate_sms(
            client=client,
            session_factory=verification_factory,
            settings=settings,
            sync_marker=MemorySmsSyncMarker(),
            now_factory=lambda: datetime.now(UTC),
            boot_id="conflict-e2e",
        )
    assert imported["correlated"] == 1, imported
    result = await reconcile_pending_sms(
        session_factory=verification_factory,
        provider=_SmsProvider(
            _sms_comparisons(date_expression="13 сентября", relation="conflicts")
        ),
        settings=settings,
    )
    assert result == {"picked": 1, "done": 0, "failed": 1, "skipped": 0}
    async with verification_factory() as db:
        call = await db.get(CommunicationSession, session_id)
        date_fact = await db.scalar(
            select(CallFact).where(
                CallFact.session_id == session_id, CallFact.field == "interview_date"
            )
        )
    assert call is not None and call.verification_status is PhoneVerificationStatus.NEEDS_REVIEW
    assert date_fact is not None
    assert date_fact.state is CallFactState.CONFLICT
    assert date_fact.confirmation_source is None


@pytest.mark.asyncio
async def test_stale_worker_lease_and_transient_provider_are_recoverable(
    verification_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "lease", telegram=False)
    fake = FakePhoneGate()
    session_id = await _run_call(verification_factory, settings, fake)
    async with verification_factory() as db:
        call = await db.get(CommunicationSession, session_id)
        assert call is not None
        call.summary_state = PhoneSummaryState.PROCESSING
        call.claim_token = "stale-worker"
        call.processing_started_at = datetime.now(UTC) - timedelta(seconds=3600)
        await db.commit()
    quote = "Собеседование 12 сентября в 14:30, улица Индепенденцей 10"
    await _finalize_call(
        verification_factory,
        session_id,
        settings,
        quote,
        monkeypatch,
        transient_provider_error=True,
    )
    async with verification_factory() as db:
        call = await db.get(CommunicationSession, session_id)
    assert call is not None
    assert call.summary_state is PhoneSummaryState.DONE
    assert call.summary["verification"]["pass_metadata"]["extractor"]["attempts"] == 2
    assert call.claim_token is None and call.processing_started_at is None


@pytest.mark.asyncio
async def test_sms_sync_failure_keeps_history_for_next_recovery_poll(
    verification_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "sms-recovery", telegram=False)
    fake = FakePhoneGate()
    session_id = await _run_call(verification_factory, settings, fake)
    async with verification_factory() as db:
        call = await db.get(CommunicationSession, session_id)
        assert call is not None and call.ended_at is not None
        ended_at = call.ended_at.replace(tzinfo=UTC)
    fake.add_sms(
        id="sms-recovery-1",
        address="+37360000000",
        text="Подтверждаем 12 сентября",
        timestamp=int(ended_at.timestamp() * 1000),
    )
    fake.fail_next_sms_sync()
    with pytest.raises(PhoneGateUnavailable):
        async with PhoneGateClient(
            base_url="http://phonegate", token="test", transport=fake.transport()
        ) as client:
            await ingest_phonegate_sms(
                client=client,
                session_factory=verification_factory,
                settings=settings,
                sync_marker=MemorySmsSyncMarker(),
                now_factory=lambda: datetime.now(UTC),
                boot_id="sms-recovery-e2e",
            )
    async with verification_factory() as db:
        assert (
            await db.scalar(
                select(CommunicationSession).where(
                    CommunicationSession.transport_external_id == "sms-recovery-1"
                )
            )
            is None
        )
    async with PhoneGateClient(
        base_url="http://phonegate", token="test", transport=fake.transport()
    ) as client:
        recovered = await ingest_phonegate_sms(
            client=client,
            session_factory=verification_factory,
            settings=settings,
            sync_marker=MemorySmsSyncMarker(),
            now_factory=lambda: datetime.now(UTC),
            boot_id="sms-recovery-e2e",
        )
    assert recovered["imported"] == 1 and recovered["correlated"] == 1


@pytest.mark.asyncio
async def test_evidence_failure_does_not_abort_call(
    verification_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    settings = _settings(tmp_path / "evidence-failure", telegram=False)
    fake = FakePhoneGate()
    session_id = await _run_call(verification_factory, settings, fake, fail_audio=True)
    async with verification_factory() as db:
        call = await db.get(CommunicationSession, session_id)
        turn = await db.scalar(
            select(CommunicationTurn).where(
                CommunicationTurn.session_id == session_id,
                CommunicationTurn.speaker == TurnSpeaker.EMPLOYER,
            )
        )
    assert call is not None and call.script_stage == "greeting_completed"
    assert turn is not None and turn.audio_evidence_path is None


@pytest.mark.asyncio
async def test_telegram_429_is_visible_and_a_followup_succeeds() -> None:
    from app.phone.telegram import TelegramDeliveryError, send_telegram_message

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TelegramDeliveryError) as caught:
        await send_telegram_message(token="bot", chat_id="chat", text="status", client=client)
    assert caught.value.retryable is True and caught.value.retry_after == 1
    result = await send_telegram_message(token="bot", chat_id="chat", text="status", client=client)
    await client.aclose()
    assert result.message_id == 7 and calls == 2
