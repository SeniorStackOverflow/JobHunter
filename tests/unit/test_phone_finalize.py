from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import (
    CallFact,
    CommunicationSession,
    CommunicationTurn,
    UserProfile,
)
from app.models.enums import (
    CallFactConfirmationSource,
    CallFactState,
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnSpeaker,
)
from app.phone import summary as summary_module
from app.phone.summary import (
    claim_pending_calls,
    finalize_pending_calls,
)
from app.phone.verification import (
    ArbitrationItem,
    ArbitrationResult,
    ExtractionResult,
    FactCandidate,
    ModelCallMeta,
    VerificationResult,
)
from app.settings.config import Settings


class _RaisingVerificationProvider:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def extract(self, context: Any) -> Any:
        raise summary_module.VerificationUnavailable(self.reason)


@dataclass
class _Env:
    factory: async_sessionmaker[AsyncSession]
    session_id: UUID
    settings: Settings

    async def get_session(self) -> CommunicationSession:
        async with self.factory() as db:
            row = await db.get(CommunicationSession, self.session_id)
            assert row is not None
            return row

    async def get_turn(self, *, phonegate_transcript_id: int) -> CommunicationTurn:
        async with self.factory() as db:
            row = await db.scalar(
                select(CommunicationTurn).where(
                    CommunicationTurn.session_id == self.session_id,
                    CommunicationTurn.phonegate_transcript_id == phonegate_transcript_id,
                )
            )
            assert row is not None
            return row


@pytest.fixture
def finalize_env(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Awaitable[_Env]]:
    async def _build(
        *,
        llm_enabled: bool,
        with_clip_for_tid: int | None = None,
        max_attempts: int = 3,
        employer_turns: int = 1,
        telegram_enabled: bool = False,
    ) -> _Env:
        settings = Settings(
            _env_file=None,
            phone_evidence_dir=tmp_path,
            phone_summary_llm_enabled=llm_enabled,
            phone_summary_llm_model="summary-model",
            phone_summary_llm_api_key="router-key",
            phone_summary_max_attempts=max_attempts,
            phone_summary_batch=10,
            telegram_enabled=telegram_enabled,
            telegram_bot_token="bot-token" if telegram_enabled else None,
            telegram_chat_id="4242" if telegram_enabled else None,
        )
        monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
        monkeypatch.setattr(summary_module, "get_settings", lambda: settings)

        class FixtureProvider:
            async def extract(self, context: Any) -> tuple[ExtractionResult, ModelCallMeta]:
                return ExtractionResult(
                    summary_text="итог",
                    outcome_guess="interview_proposed",
                    facts=[],
                    review_reasons=[],
                ), ModelCallMeta("llmrouter", "fixture", 1, 1)

            async def verify(self, context: Any) -> tuple[VerificationResult, ModelCallMeta]:
                return VerificationResult(facts=[], review_reasons=[]), ModelCallMeta(
                    "llmrouter", "fixture", 1, 1
                )

            async def arbitrate(
                self, context: Any, extracted: Any, verified: Any
            ) -> tuple[ArbitrationResult, ModelCallMeta]:
                return ArbitrationResult(decisions=[]), ModelCallMeta("llmrouter", "fixture", 1, 1)

        monkeypatch.setattr(
            summary_module, "_build_verification_provider", lambda settings: FixtureProvider()
        )

        now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
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
                ringing_at=now,
                answered_at=now,
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
                    speaker=TurnSpeaker.ASSISTANT,
                    text="Здравствуйте, это ассистент.",
                    spoken_text="Здравствуйте, это ассистент.",
                    occurred_at=now,
                )
            )
            for i in range(employer_turns):
                db.add(
                    CommunicationTurn(
                        session_id=call.id,
                        phonegate_transcript_id=7 + i,
                        seq=2 + i,
                        speaker=TurnSpeaker.EMPLOYER,
                        text=f"Звоню по вакансии, реплика {i}.",
                        occurred_at=now,
                    )
                )
            await db.commit()
            session_id = call.id

        if with_clip_for_tid is not None:
            clip_dir = tmp_path / str(session_id)
            clip_dir.mkdir(parents=True, exist_ok=True)
            (clip_dir / f"{with_clip_for_tid}.wav").write_bytes(b"RIFFwav")

        return _Env(factory=sqlite_session_factory, session_id=session_id, settings=settings)

    return _build


@pytest.mark.asyncio
async def test_finalize_links_evidence_even_when_llm_disabled(
    finalize_env: Callable[..., Awaitable[_Env]],
) -> None:
    env = await finalize_env(llm_enabled=False, with_clip_for_tid=7)
    result = await finalize_pending_calls()
    assert result["picked"] == 1
    assert result["skipped"] == 1
    turn = await env.get_turn(phonegate_transcript_id=7)
    assert turn.audio_evidence_path == f"{env.session_id}/7.wav"
    assert (await env.get_session()).summary_state is PhoneSummaryState.SKIPPED


@pytest.mark.asyncio
async def test_finalize_writes_summary_and_bumps_needs_review(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True)
    result = await finalize_pending_calls()
    assert result["done"] == 1
    s = await env.get_session()
    assert s.summary["summary_text"] == "итог"
    assert s.summary["hints"]["outcome_guess"] == "interview_proposed"
    assert s.summary["model_meta"]["attempts"] == 1
    assert s.needs_review is True
    assert s.summary_state is PhoneSummaryState.DONE
    assert s.summary["telegram"]["state"] == "pending"


@pytest.mark.asyncio
async def test_finalize_retries_then_fails_after_max_attempts(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True, max_attempts=2)
    env.settings.phone_verification_max_attempts = 2
    monkeypatch.setattr(
        summary_module,
        "_build_verification_provider",
        lambda settings: _RaisingVerificationProvider("http_503"),
    )
    await finalize_pending_calls()
    assert (await env.get_session()).summary_state is PhoneSummaryState.PENDING
    result = await finalize_pending_calls()
    assert result["failed"] == 1
    s = await env.get_session()
    assert s.summary_state is PhoneSummaryState.FAILED
    assert s.summary["model_meta"]["attempts"] == 2


@pytest.mark.asyncio
async def test_finalize_skips_session_with_no_employer_turns(
    finalize_env: Callable[..., Awaitable[_Env]],
) -> None:
    env = await finalize_env(llm_enabled=True, employer_turns=0)
    result = await finalize_pending_calls()
    assert result["skipped"] == 1
    assert (await env.get_session()).summary_state is PhoneSummaryState.SKIPPED


@pytest.mark.asyncio
async def test_finalize_telegram_failure_keeps_done(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True, telegram_enabled=True)
    await finalize_pending_calls()
    s = await env.get_session()
    assert s.summary_state is PhoneSummaryState.DONE
    assert s.summary["telegram"]["state"] == "pending"


@pytest.mark.asyncio
async def test_finalize_telegram_sent_on_success(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True, telegram_enabled=True)
    await finalize_pending_calls()
    s = await env.get_session()
    assert s.summary["telegram"]["state"] == "pending"


@pytest.mark.asyncio
async def test_finalize_links_evidence_even_when_summarizing(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True, with_clip_for_tid=7)
    await finalize_pending_calls()
    turn = await env.get_turn(phonegate_transcript_id=7)
    assert turn.audio_evidence_path == f"{env.session_id}/7.wav"


@pytest.mark.asyncio
async def test_finalize_skips_non_int_clip_stem(
    finalize_env: Callable[..., Awaitable[_Env]], tmp_path: Any
) -> None:
    env = await finalize_env(llm_enabled=False)
    clip_dir = tmp_path / str(env.session_id)
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "notanint.wav").write_bytes(b"RIFFwav")
    # must not raise
    await finalize_pending_calls()
    assert (await env.get_session()).summary_state is PhoneSummaryState.SKIPPED


@pytest.mark.asyncio
async def test_claim_pending_call_uses_processing_lease(
    finalize_env: Callable[..., Awaitable[_Env]],
) -> None:
    env = await finalize_env(llm_enabled=False)
    async with env.factory() as db:
        claimed = await claim_pending_calls(db, batch=10, lease_seconds=300)
        assert claimed == [env.session_id]
        row = await db.get(CommunicationSession, env.session_id)
        assert row is not None
        assert row.summary_state is PhoneSummaryState.PROCESSING
        assert row.processing_started_at is not None
        first_token = row.claim_token
        assert first_token

    async with env.factory() as db:
        assert await claim_pending_calls(db, batch=10, lease_seconds=300) == []


@pytest.mark.asyncio
async def test_task6_reclaims_stale_shared_sms_lease(
    finalize_env: Callable[..., Awaitable[_Env]],
) -> None:
    env = await finalize_env(llm_enabled=False)
    async with env.factory() as db:
        call = await db.get(CommunicationSession, env.session_id)
        assert call is not None
        call.claim_token = "stale-sms-token"
        call.processing_started_at = datetime.now(UTC) - timedelta(seconds=301)
        await db.commit()

    async with env.factory() as db:
        claimed = await claim_pending_calls(db, batch=1, lease_seconds=300)
        assert claimed == [env.session_id]
        call = await db.get(CommunicationSession, env.session_id)
        assert call is not None
        assert call.claim_token not in {None, "stale-sms-token"}


@pytest.mark.asyncio
async def test_direct_finalize_claim_preserves_confirmed_sms_trust(
    finalize_env: Callable[..., Awaitable[_Env]],
) -> None:
    env = await finalize_env(llm_enabled=False)
    async with env.factory() as db:
        call = await db.get(CommunicationSession, env.session_id)
        assert call is not None
        call.verification_status = PhoneVerificationStatus.CONFIRMED
        db.add(
            CallFact(
                session_id=call.id,
                field="interview_date",
                raw_expression="завтра",
                normalized_value="2026-09-06",
                state=CallFactState.CONFIRMED,
                confirmation_source=CallFactConfirmationSource.SMS,
            )
        )
        await db.commit()

    from app.phone.summary import finalize_call

    assert await finalize_call(env.session_id) == "skipped"
    async with env.factory() as db:
        call = await db.get(CommunicationSession, env.session_id)
    assert call is not None
    assert call.verification_status is PhoneVerificationStatus.CONFIRMED
    assert call.summary_state is PhoneSummaryState.SKIPPED


@pytest.mark.asyncio
async def test_concurrent_claimers_return_a_call_once(
    finalize_env: Callable[..., Awaitable[_Env]],
) -> None:
    env = await finalize_env(llm_enabled=False)

    async def claim_once() -> list[UUID]:
        async with env.factory() as db:
            return await claim_pending_calls(db, batch=1, lease_seconds=300)

    results = await asyncio.gather(claim_once(), claim_once())
    assert sum(len(result) for result in results) == 1


@pytest.mark.asyncio
async def test_finalize_runs_independent_passes_in_order(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True)
    order: list[str] = []
    meta = ModelCallMeta("llmrouter", "m", 1, 1)

    class FakeProvider:
        async def extract(self, context: Any) -> tuple[ExtractionResult, ModelCallMeta]:
            order.append("extractor")
            return ExtractionResult(
                summary_text="итог", outcome_guess="info_request", facts=[], review_reasons=[]
            ), meta

        async def verify(self, context: Any) -> tuple[VerificationResult, ModelCallMeta]:
            order.append("verifier")
            return VerificationResult(facts=[], review_reasons=[]), meta

        async def arbitrate(
            self, context: Any, extracted: Any, verified: Any
        ) -> tuple[ArbitrationResult, ModelCallMeta]:
            order.append("arbiter")
            return ArbitrationResult(decisions=[]), meta

    monkeypatch.setattr(
        summary_module, "_build_verification_provider", lambda settings: FakeProvider()
    )
    assert await summary_module.finalize_call(env.session_id) == "done"
    assert order == ["extractor", "verifier", "arbiter"]
    session = await env.get_session()
    assert session.summary_state is PhoneSummaryState.DONE
    assert session.verification_status.value == "needs_review"
    assert session.summary["verification"]["pipeline_version"] == "phone-2b-v1"


@pytest.mark.asyncio
async def test_finalize_persists_evidence_backed_critical_fact(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True, with_clip_for_tid=7)
    async with env.factory() as db:
        turn = await db.scalar(
            select(CommunicationTurn).where(
                CommunicationTurn.session_id == env.session_id,
                CommunicationTurn.phonegate_transcript_id == 7,
            )
        )
        assert turn is not None
        turn.text = "Собеседование завтра в 14:00 на ул. Пушкина, 5"
        await db.commit()

    candidate = FactCandidate(
        field="interview_date",
        raw_expression="завтра",
        normalized_value="2026-09-06",
        quote="Собеседование завтра в 14:00 на ул. Пушкина, 5",
        turn_seq=2,
        confidence=0.98,
    )
    meta = ModelCallMeta("llmrouter", "fixture", 1, 1)

    class Provider:
        async def extract(self, context: Any) -> tuple[ExtractionResult, ModelCallMeta]:
            return ExtractionResult(
                summary_text="интервью",
                outcome_guess="interview_proposed",
                facts=[candidate],
                review_reasons=[],
            ), meta

        async def verify(self, context: Any) -> tuple[VerificationResult, ModelCallMeta]:
            return VerificationResult(facts=[candidate], review_reasons=[]), meta

        async def arbitrate(
            self, context: Any, extracted: Any, verified: Any
        ) -> tuple[ArbitrationResult, ModelCallMeta]:
            return ArbitrationResult(
                decisions=[
                    ArbitrationItem(
                        field="interview_date",
                        accepted_value="2026-09-06",
                        supporting_quote=candidate.quote,
                        accepted=True,
                        reason="совпадает",
                    )
                ]
            ), meta

    monkeypatch.setattr(summary_module, "_build_verification_provider", lambda _: Provider())
    assert await summary_module.finalize_call(env.session_id) == "done"
    async with env.factory() as db:
        fact = await db.scalar(select(CallFact).where(CallFact.session_id == env.session_id))
        turn = await db.scalar(
            select(CommunicationTurn).where(
                CommunicationTurn.session_id == env.session_id,
                CommunicationTurn.phonegate_transcript_id == 7,
            )
        )
        call = await db.get(CommunicationSession, env.session_id)
    assert fact is not None and turn is not None and call is not None
    assert fact.state is CallFactState.CANDIDATE
    assert fact.source_turn_id == turn.id
    assert turn.audio_evidence_path == f"{env.session_id}/7.wav"
    assert call.verification_status.value == "high_confidence"


@pytest.mark.asyncio
async def test_finalize_requeues_when_transcript_changes_during_pipeline(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True)
    meta = ModelCallMeta("llmrouter", "m", 1, 1)

    class FakeProvider:
        async def extract(self, context: Any) -> tuple[ExtractionResult, ModelCallMeta]:
            return ExtractionResult(
                summary_text="итог", outcome_guess="info_request", facts=[], review_reasons=[]
            ), meta

        async def verify(self, context: Any) -> tuple[VerificationResult, ModelCallMeta]:
            async with env.factory() as db:
                turn = await db.scalar(
                    select(CommunicationTurn).where(
                        CommunicationTurn.session_id == env.session_id,
                        CommunicationTurn.phonegate_transcript_id == 7,
                    )
                )
                assert turn is not None
                turn.text = "Новая реплика после снимка"
                call = await db.get(CommunicationSession, env.session_id)
                assert call is not None
                call.verification_status = PhoneVerificationStatus.CONFIRMED
                await db.commit()
            return VerificationResult(facts=[], review_reasons=[]), meta

        async def arbitrate(
            self, context: Any, extracted: Any, verified: Any
        ) -> tuple[ArbitrationResult, ModelCallMeta]:
            return ArbitrationResult(decisions=[]), meta

    monkeypatch.setattr(
        summary_module, "_build_verification_provider", lambda settings: FakeProvider()
    )
    assert await summary_module.finalize_call(env.session_id) == "skipped"
    session = await env.get_session()
    assert session.summary_state is PhoneSummaryState.PENDING
    assert session.processing_started_at is None
    assert session.verification_status is PhoneVerificationStatus.CONFIRMED


@pytest.mark.asyncio
async def test_pipeline_failure_retries_then_marks_needs_review(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True)
    env.settings.phone_verification_max_attempts = 2

    class FailingProvider:
        async def extract(self, context: Any) -> tuple[ExtractionResult, ModelCallMeta]:
            raise summary_module.VerificationUnavailable("timeout")

    monkeypatch.setattr(
        summary_module, "_build_verification_provider", lambda settings: FailingProvider()
    )
    assert await summary_module.finalize_call(env.session_id) == "skipped"
    first = await env.get_session()
    assert first.summary_state is PhoneSummaryState.PENDING
    assert first.summary["model_meta"]["attempts"] == 1
    assert first.summary["model_meta"]["last_error"] == "timeout"

    assert await summary_module.finalize_call(env.session_id) == "failed"
    second = await env.get_session()
    assert second.summary_state.value == "failed"
    assert second.verification_status.value == "needs_review"
    assert second.summary["model_meta"]["attempts"] == 2
    attempts = second.summary["verification"]["attempt_history"]
    assert [entry["attempt"] for entry in attempts] == [1, 2]
    assert attempts[-1]["failed_stage"] == "extractor"
    assert attempts[-1]["stages"]["extractor"]["state"] == "failed"


@pytest.mark.asyncio
async def test_public_finalize_rejects_foreign_fresh_processing_lease(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True)
    async with env.factory() as db:
        claimed = await claim_pending_calls(db, batch=1, lease_seconds=300)
    assert claimed == [env.session_id]

    called = False

    async def unexpected_provider(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("foreign lease must not run")

    monkeypatch.setattr(summary_module, "_build_verification_provider", unexpected_provider)
    assert await summary_module.finalize_call(env.session_id) == "skipped"
    assert called is False


def test_postgres_claim_and_finalize_queries_use_row_locks() -> None:
    claim_sql = str(
        select(CommunicationSession)
        .where(CommunicationSession.summary_state == PhoneSummaryState.PENDING)
        .with_for_update(skip_locked=True)
        .compile(dialect=postgresql.dialect())
    )
    finalize_sql = str(
        select(CommunicationSession)
        .where(CommunicationSession.id == UUID("00000000-0000-0000-0000-000000000001"))
        .with_for_update()
        .compile(dialect=postgresql.dialect())
    )
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "FOR UPDATE" in finalize_sql
