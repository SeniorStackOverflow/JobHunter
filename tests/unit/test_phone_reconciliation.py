from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.models.enums import CallFactState, PhoneVerificationStatus
from app.phone.reconciliation import reconcile_verification
from app.phone.verification import (
    ArbitrationItem,
    ArbitrationResult,
    ExtractionResult,
    FactCandidate,
    VerificationContext,
    VerificationResult,
    VerificationTurn,
)

TURN_ID = UUID("11111111-1111-1111-1111-111111111111")


def _candidate(
    *,
    raw: str = "завтра",
    normalized: str | None = "2026-09-07",
    quote: str = "Собеседование завтра в 14:30",
    seq: int | None = 1,
    confidence: float = 0.95,
) -> FactCandidate:
    return FactCandidate(
        field="interview_date",
        raw_expression=raw,
        normalized_value=normalized,
        quote=quote,
        turn_seq=seq,
        confidence=confidence,
    )


def _inputs(
    *,
    extracted: list[FactCandidate] | None = None,
    verified: list[FactCandidate] | None = None,
    arbitration: list[ArbitrationItem] | None = None,
    transcript: list[VerificationTurn] | None = None,
    evidence: list[UUID] | None = None,
) -> tuple[
    VerificationContext,
    ExtractionResult,
    VerificationResult,
    ArbitrationResult,
    list[UUID],
]:
    context = VerificationContext(
        call_id="call",
        call_started_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
        timezone="Europe/Chisinau",
        transcript=transcript
        or [
            VerificationTurn(
                seq=1,
                speaker="employer",
                text="Собеседование завтра в 14:30",
                asr_confidence=0.95,
                evidence_reference=str(TURN_ID),
            )
        ],
    )
    extraction = ExtractionResult(
        summary_text="итог",
        outcome_guess="interview_proposed",
        facts=extracted if extracted is not None else [_candidate()],
        review_reasons=[],
    )
    verification = VerificationResult(
        facts=verified if verified is not None else [_candidate()],
        review_reasons=[],
    )
    arbiter = ArbitrationResult(
        decisions=arbitration
        if arbitration is not None
        else [
            ArbitrationItem(
                field="interview_date",
                accepted_value="2026-09-07",
                supporting_quote="Собеседование завтра в 14:30",
                accepted=True,
                reason="совпадает",
            )
        ]
    )
    return context, extraction, verification, arbiter, [TURN_ID] if evidence is None else evidence


def _decide(**kwargs):
    context, extracted, verified, arbitration, evidence = _inputs(**kwargs)
    return reconcile_verification(
        context=context,
        extracted=extracted,
        verified=verified,
        arbitration=arbitration,
        asr_floor=0.8,
        evidence_turn_ids=evidence,
    )


def test_independent_agreement_with_quote_and_evidence_is_high_confidence() -> None:
    decision = _decide()
    assert decision.status is PhoneVerificationStatus.HIGH_CONFIDENCE
    assert decision.facts[0].state is CallFactState.CANDIDATE
    assert decision.facts[0].normalized_value == "2026-09-07"
    assert decision.facts[0].source_turn_id == TURN_ID


def test_quote_absence_keeps_fact_unknown_for_review() -> None:
    candidate = _candidate(quote="этой фразы нет")
    decision = _decide(extracted=[candidate], verified=[candidate])
    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert decision.facts[0].state is CallFactState.UNKNOWN
    assert "quote" in decision.facts[0].reason


def test_differing_normalized_dates_are_conflict() -> None:
    old = _candidate(raw="завтра", normalized="2026-09-07")
    new = _candidate(raw="послезавтра", normalized="2026-09-08", quote="Собеседование послезавтра")
    decision = _decide(extracted=[old], verified=[new])
    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert decision.facts[0].state is CallFactState.CONFLICT


def test_arbiter_rejection_is_conflict() -> None:
    decision = _decide(
        arbitration=[
            ArbitrationItem(
                field="interview_date",
                accepted_value=None,
                supporting_quote="Собеседование завтра в 14:30",
                accepted=False,
                reason="неоднозначно",
            )
        ]
    )
    assert decision.facts[0].state is CallFactState.CONFLICT
    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW


def test_explicit_low_asr_confidence_is_unknown() -> None:
    transcript = [
        VerificationTurn(
            seq=1,
            speaker="employer",
            text="Собеседование завтра в 14:30",
            asr_confidence=0.2,
            evidence_reference=str(TURN_ID),
        )
    ]
    decision = _decide(transcript=transcript)
    assert decision.facts[0].state is CallFactState.UNKNOWN
    assert "asr" in decision.facts[0].reason


def test_missing_asr_confidence_is_eligible_with_complete_evidence() -> None:
    transcript = [
        VerificationTurn(
            seq=1,
            speaker="employer",
            text="Собеседование завтра в 14:30",
            evidence_reference=str(TURN_ID),
        )
    ]
    decision = _decide(transcript=transcript)
    assert decision.facts[0].state is CallFactState.CANDIDATE


def test_missing_evidence_is_unknown() -> None:
    decision = _decide(evidence=[])
    assert decision.facts[0].state is CallFactState.UNKNOWN
    assert "evidence" in decision.facts[0].reason


def test_last_unambiguous_correction_wins_and_prior_expression_is_diagnostic() -> None:
    first_id = uuid4()
    second_id = uuid4()
    transcript = [
        VerificationTurn(
            seq=1,
            speaker="employer",
            text="Собеседование завтра в 14:30",
            asr_confidence=0.95,
            evidence_reference=str(first_id),
        ),
        VerificationTurn(
            seq=2,
            speaker="employer",
            text="Нет, точнее послезавтра в 14:30",
            asr_confidence=0.95,
            evidence_reference=str(second_id),
        ),
    ]
    first = _candidate(quote="Собеседование завтра в 14:30")
    corrected = _candidate(
        raw="послезавтра",
        normalized="2026-09-08",
        quote="Нет, точнее послезавтра в 14:30",
        seq=2,
    )
    decision = _decide(
        extracted=[first, corrected],
        verified=[first, corrected],
        transcript=transcript,
        evidence=[first_id, second_id],
        arbitration=[
            ArbitrationItem(
                field="interview_date",
                accepted_value="2026-09-08",
                supporting_quote="Нет, точнее послезавтра в 14:30",
                accepted=True,
                reason="исправление",
            )
        ],
    )
    assert decision.status is PhoneVerificationStatus.HIGH_CONFIDENCE
    assert decision.facts[0].raw_expression == "послезавтра"
    assert any("2026-09-07" in reason for reason in decision.reasons)


def test_address_difference_in_case_and_whitespace_is_equal() -> None:
    first = FactCandidate(
        field="address",
        raw_expression="ул. Пушкина, 5",
        normalized_value="ул. Пушкина, 5",
        quote="ул. Пушкина, 5",
        turn_seq=1,
        confidence=0.9,
    )
    second = first.model_copy(
        update={
            "raw_expression": "УЛ.  ПУШКИНА,  5",
            "normalized_value": "УЛ.  ПУШКИНА,  5",
            "quote": "ул. Пушкина, 5",
        }
    )
    context, _, _, _, evidence = _inputs(extracted=[first], verified=[second])
    context = context.model_copy(
        update={
            "transcript": [
                VerificationTurn(
                    seq=1,
                    speaker="employer",
                    text="ул. Пушкина, 5",
                    evidence_reference=str(TURN_ID),
                )
            ]
        }
    )
    decision = reconcile_verification(
        context=context,
        extracted=ExtractionResult(
            summary_text="", outcome_guess="unclear", facts=[first], review_reasons=[]
        ),
        verified=VerificationResult(facts=[second], review_reasons=[]),
        arbitration=ArbitrationResult(
            decisions=[
                ArbitrationItem(
                    field="address",
                    accepted_value="УЛ.  ПУШКИНА,  5",
                    supporting_quote="УЛ.  ПУШКИНА,  5",
                    accepted=True,
                    reason="совпадает",
                )
            ]
        ),
        asr_floor=0.8,
        evidence_turn_ids=evidence,
    )
    assert decision.facts[0].state is CallFactState.CANDIDATE


def test_vague_time_remains_unknown() -> None:
    candidate = _candidate(raw="после обеда", normalized=None, quote="Собеседование после обеда")
    decision = _decide(extracted=[candidate], verified=[candidate])
    assert decision.facts[0].state is CallFactState.UNKNOWN
    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
