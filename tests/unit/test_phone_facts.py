from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CallFact, CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import (
    CallFactConfirmationSource,
    CallFactState,
    CommunicationChannel,
    CommunicationDirection,
    PhoneVerificationStatus,
    TurnSpeaker,
)
from app.phone.facts import replace_current_facts
from app.phone.reconciliation import ReconciledFact, VerificationDecision
from app.phone.verification import ModelCallMeta, VerificationResult

TURN_ID = UUID("22222222-2222-2222-2222-222222222222")


def _decision(value: str = "2026-09-07") -> VerificationDecision:
    return VerificationDecision(
        status=PhoneVerificationStatus.HIGH_CONFIDENCE,
        facts=(
            ReconciledFact(
                field="interview_date",
                raw_expression="завтра",
                normalized_value=value,
                source_turn_id=TURN_ID,
                asr_confidence=0.95,
                llm_confidence=0.95,
                state=CallFactState.CANDIDATE,
                reason="independent agreement",
            ),
        ),
        reasons=(),
    )


@pytest.mark.asyncio
async def test_replace_current_facts_is_idempotent_and_keeps_history(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address="",
            remote_raw="",
            phonegate_event_id_start=1,
            started_at=datetime.now(UTC),
        )
        db.add(call)
        await db.flush()
        db.add(
            CommunicationTurn(
                id=TURN_ID,
                session_id=call.id,
                seq=1,
                speaker=TurnSpeaker.EMPLOYER,
                text="Собеседование завтра",
                occurred_at=datetime.now(UTC),
            )
        )
        await db.flush()
        meta = {"extractor": ModelCallMeta("llmrouter", "m", 1, 1)}
        results = {"extractor": VerificationResult(facts=[], review_reasons=[])}

        await replace_current_facts(
            db,
            call=call,
            decision=_decision(),
            pass_metadata=meta,
            input_fingerprint="input-a",
            pipeline_version="v1",
            pass_results=results,
        )
        await db.flush()
        assert call.verification_revision == 1
        await replace_current_facts(
            db,
            call=call,
            decision=_decision(),
            pass_metadata=meta,
            input_fingerprint="input-a",
            pipeline_version="v1",
            pass_results=results,
        )
        await db.flush()
        rows = (
            (await db.execute(select(CallFact).where(CallFact.session_id == call.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert call.verification_revision == 1

        await replace_current_facts(
            db,
            call=call,
            decision=_decision("2026-09-08"),
            pass_metadata=meta,
            input_fingerprint="input-b",
            pipeline_version="v1",
            pass_results=results,
        )
        await db.flush()
        assert call.verification_revision == 2
        row = (
            await db.execute(select(CallFact).where(CallFact.session_id == call.id))
        ).scalar_one()
        assert row.normalized_value == "2026-09-08"
        verification = call.summary["verification"]
        assert verification["input_fingerprint"] == "input-b"
        assert len(verification["history"]) == 1
        assert verification["history"][0]["input_fingerprint"] == "input-a"
        assert verification["pass_results"]["extractor"]["review_reasons"] == []


@pytest.mark.asyncio
async def test_model_retry_preserves_sms_confirmation(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address="",
            remote_raw="",
            phonegate_event_id_start=1,
            started_at=datetime.now(UTC),
        )
        db.add(call)
        await db.flush()
        existing = CallFact(
            session_id=call.id,
            field="interview_date",
            raw_expression="завтра",
            normalized_value="2026-09-07",
            state=CallFactState.CONFIRMED,
            confirmation_source=CallFactConfirmationSource.SMS,
        )
        db.add(existing)
        await db.flush()

        await replace_current_facts(
            db,
            call=call,
            decision=_decision("2026-09-08"),
            pass_metadata={},
            input_fingerprint="retry",
            pipeline_version="v1",
            pass_results={},
        )
        await db.flush()
        row = (
            await db.execute(select(CallFact).where(CallFact.session_id == call.id))
        ).scalar_one()
        assert row.confirmation_source is CallFactConfirmationSource.SMS
        assert row.state is CallFactState.CONFIRMED
        assert row.normalized_value == "2026-09-07"
