from __future__ import annotations

# ruff: noqa: RUF001 — Russian correction phrases are intentional test data.
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
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnSpeaker,
)
from app.phone.facts import (
    SmsConfirmationRejected,
    apply_sms_confirmation,
    replace_current_facts,
    unlink_sms_confirmation,
)
from app.phone.reconciliation import ReconciledFact, VerificationDecision
from app.phone.verification import (
    ModelCallMeta,
    SmsComparisonResult,
    SmsFieldComparison,
    VerificationResult,
)

TURN_ID = UUID("22222222-2222-2222-2222-222222222222")
SMS_TURN_ID = UUID("33333333-3333-3333-3333-333333333333")
SMS2_TURN_ID = UUID("44444444-4444-4444-4444-444444444444")


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


async def _call_with_sms(
    db: AsyncSession,
    profile: UserProfile,
    *,
    call_state: CallFactState = CallFactState.CANDIDATE,
) -> tuple[CommunicationSession, CommunicationSession, CallFact]:
    call = CommunicationSession(
        profile_id=profile.id,
        channel=CommunicationChannel.CALL,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        remote_address="+37360000000",
        remote_raw="+37360000000",
        phonegate_event_id_start=1,
        started_at=datetime(2026, 9, 6, 21, 0, tzinfo=UTC),
    )
    sms = CommunicationSession(
        profile_id=profile.id,
        channel=CommunicationChannel.SMS,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        remote_address="+37360000000",
        remote_raw="+37360000000",
        transport_external_id="sms-1",
        started_at=datetime(2026, 9, 6, 21, 30, tzinfo=UTC),
        ended_at=datetime(2026, 9, 6, 21, 30, tzinfo=UTC),
    )
    db.add_all([call, sms])
    await db.flush()
    sms.related_session_id = call.id
    db.add(
        CommunicationTurn(
            id=TURN_ID,
            session_id=call.id,
            seq=1,
            speaker=TurnSpeaker.EMPLOYER,
            text="Собеседование завтра в офисе",
            occurred_at=call.started_at,
        )
    )
    db.add(
        CommunicationTurn(
            id=SMS_TURN_ID,
            session_id=sms.id,
            seq=1,
            speaker=TurnSpeaker.EMPLOYER,
            text="Подтверждаем сегодня в 10:00",
            occurred_at=sms.started_at,
        )
    )
    fact = CallFact(
        session_id=call.id,
        field="interview_date",
        raw_expression="завтра",
        normalized_value="2026-09-07",
        state=call_state,
    )
    db.add(fact)
    await db.flush()
    call.verification_status = PhoneVerificationStatus.HIGH_CONFIDENCE
    return call, sms, fact


@pytest.mark.asyncio
async def test_matching_sms_confirms_only_explicitly_mentioned_fact(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call, sms, fact = await _call_with_sms(db, profile)
        other = CallFact(
            session_id=call.id,
            field="interview_time",
            raw_expression="в 11:00",
            normalized_value="11:00",
            state=CallFactState.CANDIDATE,
        )
        db.add(other)
        await db.flush()

        status = await apply_sms_confirmation(
            db,
            call=call,
            sms_session=sms,
            comparison=SmsComparisonResult(
                comparisons=[
                    SmsFieldComparison(
                        field="interview_date",
                        relation="matches",
                        sms_expression="сегодня",
                        call_expression="завтра",
                        reason="same date",
                    ),
                    SmsFieldComparison(
                        field="interview_time",
                        relation="not_mentioned",
                        sms_expression="",
                        call_expression="в 11:00",
                        reason="not present",
                    ),
                ]
            ),
            metadata=ModelCallMeta("llmrouter", "sms-model", 4, 1),
        )
        await db.commit()

    assert status is PhoneVerificationStatus.HIGH_CONFIDENCE
    assert fact.state is CallFactState.CONFIRMED
    assert fact.confirmation_source is CallFactConfirmationSource.SMS
    assert fact.confirmed_by_turn_id == SMS_TURN_ID
    assert other.state is CallFactState.CANDIDATE


@pytest.mark.asyncio
async def test_deterministic_sms_mismatch_cannot_be_overridden_by_matches(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call, sms, fact = await _call_with_sms(db, profile)
        fact.normalized_value = "2026-09-08"

        status = await apply_sms_confirmation(
            db,
            call=call,
            sms_session=sms,
            comparison=SmsComparisonResult(
                comparisons=[
                    SmsFieldComparison(
                        field="interview_date",
                        relation="matches",
                        sms_expression="сегодня",
                        call_expression="завтра",
                        reason="model says matches",
                    )
                ]
            ),
            metadata=ModelCallMeta("llmrouter", "sms-model", 4, 1),
        )
        await db.commit()

    assert status is PhoneVerificationStatus.NEEDS_REVIEW
    assert fact.state is CallFactState.CONFLICT
    assert fact.confirmation_source is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "sms_text", "sms_expression", "raw_expression", "normalized_value"),
    [
        (
            "interview_date",
            "Собеседование не завтра, а послезавтра",
            "завтра",
            "завтра",
            "2026-09-07",
        ),
        (
            "interview_time",
            "В 10:00, точнее в 11:00",
            "10:00",
            "в 10:00",
            "10:00",
        ),
        (
            "address",
            "Вместо ул. Индустриальная 12 будет проспект Дачия 4",
            "ул. Индустриальная 12",
            "ул. Индустриальная 12",
            "ул. Индустриальная 12",
        ),
        (
            "company",
            "Компания не Acme",
            "Acme",
            "Acme",
            "Acme",
        ),
    ],
)
async def test_sms_correction_or_negation_never_confirms(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    field: str,
    sms_text: str,
    sms_expression: str,
    raw_expression: str,
    normalized_value: str,
) -> None:
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call, sms, fact = await _call_with_sms(db, profile)
        fact.field = field
        fact.raw_expression = raw_expression
        fact.normalized_value = normalized_value
        turn = await db.scalar(
            select(CommunicationTurn).where(CommunicationTurn.session_id == sms.id)
        )
        assert turn is not None
        turn.text = sms_text
        status = await apply_sms_confirmation(
            db,
            call=call,
            sms_session=sms,
            comparison=SmsComparisonResult(
                comparisons=[
                    SmsFieldComparison(
                        field=field,
                        relation="matches",
                        sms_expression=sms_expression,
                        call_expression=raw_expression,
                        reason="model says matches",
                    )
                ]
            ),
            metadata=ModelCallMeta("llmrouter", "sms-model", 4, 1),
        )

    assert status is PhoneVerificationStatus.NEEDS_REVIEW
    assert fact.state is CallFactState.CANDIDATE
    assert fact.confirmation_source is None


@pytest.mark.asyncio
async def test_sms_relative_date_uses_sms_occurrence_in_chisinau_timezone(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call, sms, fact = await _call_with_sms(db, profile)
        # 20:30 UTC is still 23:30 on Sep 6 in Chisinau; the call starts at
        # 21:00 UTC and is already on Sep 7 locally.
        sms.started_at = datetime(2026, 9, 6, 20, 30, tzinfo=UTC)
        sms.ended_at = sms.started_at
        sms_turn = await db.scalar(
            select(CommunicationTurn).where(CommunicationTurn.session_id == sms.id)
        )
        assert sms_turn is not None
        sms_turn.occurred_at = sms.started_at
        fact.normalized_value = "2026-09-06"
        status = await apply_sms_confirmation(
            db,
            call=call,
            sms_session=sms,
            comparison=SmsComparisonResult(
                comparisons=[
                    SmsFieldComparison(
                        field="interview_date",
                        relation="matches",
                        sms_expression="сегодня",
                        call_expression="завтра",
                        reason="same canonical date",
                    )
                ]
            ),
            metadata=ModelCallMeta("llmrouter", "sms-model", 4, 1),
        )

    assert status is PhoneVerificationStatus.CONFIRMED
    assert fact.state is CallFactState.CONFIRMED


@pytest.mark.asyncio
async def test_same_sms_is_idempotent_and_unlink_restores_prior_fact(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call, sms, fact = await _call_with_sms(db, profile)
        comparison = SmsComparisonResult(
            comparisons=[
                SmsFieldComparison(
                    field="interview_date",
                    relation="matches",
                    sms_expression="сегодня",
                    call_expression="завтра",
                    reason="same date",
                )
            ]
        )
        meta = ModelCallMeta("llmrouter", "sms-model", 4, 1)
        first = await apply_sms_confirmation(
            db, call=call, sms_session=sms, comparison=comparison, metadata=meta
        )
        first_summary = call.summary
        first_revision = call.verification_revision
        second = await apply_sms_confirmation(
            db, call=call, sms_session=sms, comparison=comparison, metadata=meta
        )
        assert second is first
        assert call.summary == first_summary
        assert call.verification_revision == first_revision
        await unlink_sms_confirmation(db, call=call, sms_session=sms)
        await db.commit()

    assert fact.state is CallFactState.CANDIDATE
    assert fact.confirmation_source is None
    assert fact.confirmed_by_turn_id is None
    assert call.verification_status is PhoneVerificationStatus.HIGH_CONFIDENCE


@pytest.mark.asyncio
async def test_unlink_removes_sms_work_and_requeues_ended_call(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call, sms, _fact = await _call_with_sms(db, profile)
        call.auto_answered = True
        call.ended_at = call.started_at
        call.summary_state = PhoneSummaryState.DONE
        await apply_sms_confirmation(
            db,
            call=call,
            sms_session=sms,
            comparison=SmsComparisonResult(
                comparisons=[
                    SmsFieldComparison(
                        field="interview_date",
                        relation="matches",
                        sms_expression="сегодня",
                        call_expression="завтра",
                        reason="same date",
                    )
                ]
            ),
            metadata=ModelCallMeta("llmrouter", "sms-model", 4, 1),
        )
        summary = dict(call.summary)
        verification = dict(summary["verification"])
        verification["sms_pending"] = [str(SMS_TURN_ID)]
        summary["verification"] = verification
        call.summary = summary
        revision = call.verification_revision
        await unlink_sms_confirmation(db, call=call, sms_session=sms)
        verification = call.summary["verification"]

    assert call.verification_revision == revision + 1
    assert str(SMS_TURN_ID) not in verification["sms_pending"]
    assert str(SMS_TURN_ID) not in verification["sms_input_ids"]
    assert call.summary_state is PhoneSummaryState.PENDING
    assert sms.related_session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["unlinked", "outgoing"])
async def test_sms_confirmation_rejects_unlinked_or_outgoing_message(
    sqlite_session_factory: async_sessionmaker[AsyncSession], attack: str
) -> None:
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call, sms, _ = await _call_with_sms(db, profile)
        if attack == "unlinked":
            sms.related_session_id = None
        else:
            sms.direction = CommunicationDirection.OUTBOUND
        with pytest.raises(SmsConfirmationRejected):
            await apply_sms_confirmation(
                db,
                call=call,
                sms_session=sms,
                comparison=SmsComparisonResult(comparisons=[]),
                metadata=ModelCallMeta("llmrouter", "sms-model", 4, 1),
            )


@pytest.mark.asyncio
async def test_sms_confirmation_rejects_multiple_turns(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call, sms, _ = await _call_with_sms(db, profile)
        db.add(
            CommunicationTurn(
                session_id=sms.id,
                seq=2,
                speaker=TurnSpeaker.EMPLOYER,
                text="вторая поддельная часть",
                occurred_at=sms.started_at,
            )
        )
        await db.flush()
        with pytest.raises(SmsConfirmationRejected):
            await apply_sms_confirmation(
                db,
                call=call,
                sms_session=sms,
                comparison=SmsComparisonResult(comparisons=[]),
                metadata=ModelCallMeta("llmrouter", "sms-model", 4, 1),
            )


@pytest.mark.asyncio
async def test_multiple_sms_supporters_survive_single_unlink(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    comparison = SmsComparisonResult(
        comparisons=[
            SmsFieldComparison(
                field="interview_date",
                relation="matches",
                sms_expression="сегодня",
                call_expression="завтра",
                reason="same",
            )
        ]
    )
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call, first, fact = await _call_with_sms(db, profile)
        second = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.SMS,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address="+37360000000",
            remote_raw="+37360000000",
            transport_external_id="sms-2",
            related_session_id=call.id,
            started_at=first.started_at,
            ended_at=first.ended_at,
        )
        db.add(second)
        await db.flush()
        db.add(
            CommunicationTurn(
                id=SMS2_TURN_ID,
                session_id=second.id,
                seq=1,
                speaker=TurnSpeaker.EMPLOYER,
                text="Подтверждаем сегодня",
                occurred_at=second.started_at,
            )
        )
        await db.flush()
        meta = ModelCallMeta("llmrouter", "sms-model", 4, 1)
        await apply_sms_confirmation(
            db, call=call, sms_session=first, comparison=comparison, metadata=meta
        )
        await apply_sms_confirmation(
            db, call=call, sms_session=second, comparison=comparison, metadata=meta
        )
        await unlink_sms_confirmation(db, call=call, sms_session=first)
        assert fact.state is CallFactState.CONFIRMED
        assert fact.confirmed_by_turn_id == SMS2_TURN_ID
        await unlink_sms_confirmation(db, call=call, sms_session=second)

    assert fact.state is CallFactState.CANDIDATE
    assert fact.confirmation_source is None


@pytest.mark.asyncio
async def test_hallucinated_sms_expression_is_review_only(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call, sms, fact = await _call_with_sms(db, profile)
        call.verification_status = PhoneVerificationStatus.HIGH_CONFIDENCE
        status = await apply_sms_confirmation(
            db,
            call=call,
            sms_session=sms,
            comparison=SmsComparisonResult(
                comparisons=[
                    SmsFieldComparison(
                        field="interview_date",
                        relation="matches",
                        sms_expression="послезавтра",
                        call_expression="завтра",
                        reason="hallucinated",
                    )
                ]
            ),
            metadata=ModelCallMeta("llmrouter", "sms-model", 4, 1),
        )

    assert status is PhoneVerificationStatus.NEEDS_REVIEW
    assert fact.state is CallFactState.CANDIDATE


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
async def test_same_fingerprint_and_pipeline_is_a_true_noop(
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
        turn = CommunicationTurn(
            id=TURN_ID,
            session_id=call.id,
            seq=1,
            speaker=TurnSpeaker.EMPLOYER,
            text="Собеседование завтра",
            occurred_at=datetime.now(UTC),
        )
        db.add(turn)
        await db.flush()
        first = _decision("2026-09-07")
        second = _decision("2026-09-08")
        meta = {"extractor": ModelCallMeta("llmrouter", "m", 1, 1)}
        result = {"extractor": VerificationResult(facts=[], review_reasons=[])}
        await replace_current_facts(
            db,
            call=call,
            decision=first,
            pass_metadata=meta,
            input_fingerprint="same",
            pipeline_version="v1",
            pass_results=result,
        )
        await db.flush()
        before_summary = call.summary
        before_revision = call.verification_revision
        before_status = call.verification_status
        before_fact = (
            await db.execute(select(CallFact).where(CallFact.session_id == call.id))
        ).scalar_one()
        before_fact_values = (
            before_fact.raw_expression,
            before_fact.normalized_value,
            before_fact.state,
        )
        await replace_current_facts(
            db,
            call=call,
            decision=second,
            pass_metadata={"extractor": ModelCallMeta("llmrouter", "different", 99, 3)},
            input_fingerprint="same",
            pipeline_version="v1",
            pass_results={"extractor": VerificationResult(facts=[], review_reasons=["retry"])},
        )
        await db.flush()
        after_fact = (
            await db.execute(select(CallFact).where(CallFact.session_id == call.id))
        ).scalar_one()
        assert call.summary == before_summary
        assert call.verification_revision == before_revision
        assert call.verification_status is before_status
        assert (
            after_fact.raw_expression,
            after_fact.normalized_value,
            after_fact.state,
        ) == before_fact_values


@pytest.mark.asyncio
async def test_pipeline_change_creates_revision_for_same_input(
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
        kwargs = {
            "decision": _decision(),
            "pass_metadata": {},
            "input_fingerprint": "same",
            "pass_results": {},
        }
        await replace_current_facts(db, call=call, pipeline_version="v1", **kwargs)
        await db.flush()
        await replace_current_facts(db, call=call, pipeline_version="v2", **kwargs)
        await db.flush()
        assert call.verification_revision == 2
        assert len(call.summary["verification"]["history"]) == 1


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
        call.summary = {
            "verification": {
                "sms_comparisons": [{"sms_turn_id": str(SMS_TURN_ID)}],
                "sms_input_ids": [str(SMS_TURN_ID)],
            }
        }
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
        assert call.summary["verification"]["sms_input_ids"] == [str(SMS_TURN_ID)]
