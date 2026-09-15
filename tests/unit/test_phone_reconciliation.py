from __future__ import annotations

# ruff: noqa: RUF001 — Russian correction phrases are intentional test data.
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

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
                turn_id=TURN_ID,
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


@pytest.mark.parametrize("violation", [None, "whole_sentence", "translation", "missing_evidence"])
def test_field_only_russian_values_keep_all_acceptance_gates(violation: str | None) -> None:
    text = (
        "По вакансии кладовщика. Собеседование 12 сентября в 14.30, "
        "улица Индепенденцей 10, по кишинёвскому времени."
    )
    values = [
        ("vacancy", "кладовщика", "кладовщика"),
        ("interview_date", "12 сентября", "2026-09-12"),
        ("interview_time", "14.30", "14:30"),
        ("address", "улица Индепенденцей 10", "улица Индепенденцей 10"),
        ("timezone", "по кишинёвскому времени", "Europe/Chisinau"),
    ]
    extracted = [
        FactCandidate(
            field=field,
            raw_expression=raw,
            normalized_value=value,
            quote=text,
            turn_seq=1,
            confidence=0.99,
        )
        for field, raw, value in values
    ]
    verified = [item.model_copy() for item in extracted]
    if violation == "whole_sentence":
        verified = [item.model_copy(update={"raw_expression": text}) for item in verified]
    if violation == "translation":
        verified[0] = verified[0].model_copy(update={"normalized_value": "Warehouse keeper"})
    decision = _decide(
        extracted=extracted,
        verified=verified,
        arbitration=[
            ArbitrationItem(
                field=field,
                accepted_value=value,
                supporting_quote=text,
                accepted=True,
                reason="совпадает",
            )
            for field, _, value in values
        ],
        transcript=[
            VerificationTurn(
                seq=1, turn_id=TURN_ID, speaker="employer", text=text, asr_confidence=0.98
            )
        ],
        evidence=[] if violation == "missing_evidence" else [TURN_ID],
    )
    if violation is None:
        assert decision.status is PhoneVerificationStatus.HIGH_CONFIDENCE
        assert {fact.field: fact.normalized_value for fact in decision.facts} == {
            "vacancy": "кладовщика",
            "interview_date": "2026-09-12",
            "interview_time": "14:30",
            "address": "улица Индепенденцей 10",
            "timezone": "Europe/Chisinau",
        }
        assert all(fact.state is CallFactState.CANDIDATE for fact in decision.facts)
    else:
        assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
        assert any(fact.state is CallFactState.UNKNOWN for fact in decision.facts)


def test_quote_absence_keeps_fact_unknown_for_review() -> None:
    candidate = _candidate(quote="этой фразы нет")
    decision = _decide(extracted=[candidate], verified=[candidate])
    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert decision.facts[0].state is CallFactState.UNKNOWN
    assert "quote" in decision.facts[0].reason


def test_raw_expression_must_be_bound_to_the_supporting_quote() -> None:
    text = "Собеседование будет, подробности позже."
    hallucinated = _candidate(raw="12 сентября", normalized="2026-09-12", quote=text)
    decision = _decide(
        extracted=[hallucinated],
        verified=[hallucinated],
        transcript=[
            VerificationTurn(
                seq=1,
                turn_id=TURN_ID,
                speaker="employer",
                text=text,
                asr_confidence=0.95,
            )
        ],
        arbitration=[
            ArbitrationItem(
                field="interview_date",
                accepted_value="2026-09-12",
                supporting_quote=text,
                accepted=True,
                reason="совпадает",
            )
        ],
    )

    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert decision.facts[0].state is CallFactState.UNKNOWN
    assert "raw expression" in decision.facts[0].reason


def test_arbiter_quote_must_contain_the_accepted_expression() -> None:
    text = "Собеседование завтра. Подробности позже."
    candidate = _candidate(quote="Собеседование завтра.")
    decision = _decide(
        extracted=[candidate],
        verified=[candidate],
        transcript=[
            VerificationTurn(
                seq=1,
                turn_id=TURN_ID,
                speaker="employer",
                text=text,
                asr_confidence=0.95,
            )
        ],
        arbitration=[
            ArbitrationItem(
                field="interview_date",
                accepted_value="2026-09-07",
                supporting_quote="Подробности позже.",
                accepted=True,
                reason="совпадает",
            )
        ],
    )

    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert decision.facts[0].state is CallFactState.UNKNOWN
    assert "arbiter expression" in decision.facts[0].reason


@pytest.mark.parametrize("reverse", [False, True])
def test_same_turn_correction_selects_last_expression(reverse: bool) -> None:
    text = "Собеседование не 12 сентября, а 13 сентября."
    old = _candidate(raw="12 сентября", normalized="2026-09-12", quote=text)
    new = _candidate(raw="13 сентября", normalized="2026-09-13", quote=text)
    candidates = [new, old] if reverse else [old, new]
    decision = _decide(
        extracted=candidates,
        verified=[item.model_copy() for item in candidates],
        transcript=[
            VerificationTurn(
                seq=1,
                turn_id=TURN_ID,
                speaker="employer",
                text=text,
                asr_confidence=0.95,
            )
        ],
        arbitration=[
            ArbitrationItem(
                field="interview_date",
                accepted_value="2026-09-13",
                supporting_quote=text,
                accepted=True,
                reason="исправление",
            )
        ],
    )

    assert decision.status is PhoneVerificationStatus.HIGH_CONFIDENCE
    assert decision.facts[0].normalized_value == "2026-09-13"


def test_same_turn_correction_rejects_arbiter_selecting_negated_value() -> None:
    text = "Собеседование не 12 сентября, а 13 сентября."
    old = _candidate(raw="12 сентября", normalized="2026-09-12", quote=text)
    new = _candidate(raw="13 сентября", normalized="2026-09-13", quote=text)
    decision = _decide(
        extracted=[old, new],
        verified=[old.model_copy(), new.model_copy()],
        transcript=[
            VerificationTurn(
                seq=1,
                turn_id=TURN_ID,
                speaker="employer",
                text=text,
                asr_confidence=0.95,
            )
        ],
        arbitration=[
            ArbitrationItem(
                field="interview_date",
                accepted_value="2026-09-12",
                supporting_quote=text,
                accepted=True,
                reason="ошибка",
            )
        ],
    )

    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert decision.facts[0].state is CallFactState.CONFLICT


def test_correction_marker_after_date_candidates_does_not_resolve_date() -> None:
    text = "Собеседование 12 сентября или 13 сентября; точнее, адрес уточним позже."
    first = _candidate(raw="12 сентября", normalized="2026-09-12", quote=text)
    second = _candidate(raw="13 сентября", normalized="2026-09-13", quote=text)
    decision = _decide(
        extracted=[first, second],
        verified=[first.model_copy(), second.model_copy()],
        transcript=[
            VerificationTurn(
                seq=1,
                turn_id=TURN_ID,
                speaker="employer",
                text=text,
                asr_confidence=0.95,
            )
        ],
        arbitration=[
            ArbitrationItem(
                field="interview_date",
                accepted_value="2026-09-13",
                supporting_quote=text,
                accepted=True,
                reason="последнее значение",
            )
        ],
    )

    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert decision.facts[0].state is CallFactState.CONFLICT


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


def test_missing_arbiter_decision_is_unknown_for_review() -> None:
    decision = _decide(arbitration=[])
    assert decision.facts[0].state is CallFactState.UNKNOWN
    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert "arbiter" in decision.facts[0].reason


def test_blank_arbiter_quote_is_unknown_for_review() -> None:
    decision = _decide(
        arbitration=[
            ArbitrationItem(
                field="interview_date",
                accepted_value="2026-09-07",
                supporting_quote="  \n\t",
                accepted=True,
                reason="совпадает",
            )
        ]
    )
    assert decision.facts[0].state is CallFactState.UNKNOWN
    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert "arbiter quote" in decision.facts[0].reason


def test_arbiter_quote_from_different_turn_is_unknown_for_review() -> None:
    other_turn_id = uuid4()
    context, extracted, verified, _, evidence = _inputs(
        transcript=[
            VerificationTurn(
                seq=1,
                turn_id=TURN_ID,
                speaker="employer",
                text="Собеседование завтра в 14:30",
                asr_confidence=0.95,
                evidence_reference=str(TURN_ID),
            ),
            VerificationTurn(
                seq=2,
                turn_id=other_turn_id,
                speaker="employer",
                text="Это отдельная фраза для другой проверки",
                asr_confidence=0.95,
                evidence_reference=str(other_turn_id),
            ),
        ],
    )
    decision = reconcile_verification(
        context=context,
        extracted=extracted,
        verified=verified,
        arbitration=ArbitrationResult(
            decisions=[
                ArbitrationItem(
                    field="interview_date",
                    accepted_value="2026-09-07",
                    supporting_quote="Это отдельная фраза для другой проверки",
                    accepted=True,
                    reason="совпадает",
                )
            ]
        ),
        asr_floor=0.8,
        evidence_turn_ids=[*evidence, other_turn_id],
    )
    assert decision.facts[0].state is CallFactState.UNKNOWN
    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert "arbiter quote" in decision.facts[0].reason


def test_candidate_ambiguity_is_unknown_for_review() -> None:
    ambiguous = _candidate()
    ambiguous.ambiguity = "возможны два времени"
    decision = _decide(extracted=[ambiguous], verified=[ambiguous])
    assert decision.facts[0].state is CallFactState.UNKNOWN
    assert "ambiguity" in decision.facts[0].reason


def test_pass_review_reason_requires_review_but_preserves_safe_fact() -> None:
    context, extracted, verified, arbitration, evidence = _inputs()
    extracted.review_reasons.append("модель требует ручной проверки")
    decision = reconcile_verification(
        context=context,
        extracted=extracted,
        verified=verified,
        arbitration=arbitration,
        asr_floor=0.8,
        evidence_turn_ids=evidence,
    )
    assert decision.facts[0].state is CallFactState.CANDIDATE
    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert any("ручной проверки" in reason for reason in decision.reasons)


def test_explicit_low_asr_confidence_is_unknown() -> None:
    transcript = [
        VerificationTurn(
            seq=1,
            turn_id=TURN_ID,
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
            turn_id=TURN_ID,
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
            turn_id=first_id,
            speaker="employer",
            text="Собеседование завтра в 14:30",
            asr_confidence=0.95,
            evidence_reference=str(first_id),
        ),
        VerificationTurn(
            seq=2,
            turn_id=second_id,
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


def test_supported_correction_wins_over_unsupported_earlier_expression() -> None:
    first_id = uuid4()
    second_id = uuid4()
    transcript = [
        VerificationTurn(
            seq=1,
            turn_id=first_id,
            speaker="employer",
            text="Собеседование завтра, но фраза неразборчива",
            asr_confidence=0.2,
            evidence_reference=str(first_id),
        ),
        VerificationTurn(
            seq=2,
            turn_id=second_id,
            speaker="employer",
            text="Нет, точнее послезавтра в 14:30",
            asr_confidence=0.95,
            evidence_reference=str(second_id),
        ),
    ]
    earlier = _candidate(quote="Собеседование завтра")
    corrected = _candidate(
        raw="послезавтра",
        normalized="2026-09-08",
        quote="Нет, точнее послезавтра в 14:30",
        seq=2,
    )
    decision = _decide(
        extracted=[earlier, corrected],
        verified=[earlier, corrected],
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
    assert decision.facts[0].normalized_value == "2026-09-08"
    assert any("завтра" in reason for reason in decision.reasons)


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
                    turn_id=TURN_ID,
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
                    supporting_quote="ул. Пушкина, 5",
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


def test_company_prefix_and_quotes_do_not_create_fake_conflict() -> None:
    text = 'Добрый день. Компания «Компетенс Маркетинг». Перезвоните, пожалуйста.'
    extracted = FactCandidate(
        field="company",
        raw_expression="Компетенс Маркетинг",
        normalized_value="Компетенс Маркетинг",
        quote=text,
        turn_seq=1,
        confidence=1.0,
    )
    verified = FactCandidate(
        field="company",
        raw_expression='Компания «Компетенс Маркетинг»',
        normalized_value='Компания «Компетенс Маркетинг»',
        quote=text,
        turn_seq=1,
        confidence=0.99,
    )
    decision = _decide(
        extracted=[extracted],
        verified=[verified],
        arbitration=[
            ArbitrationItem(
                field="company",
                accepted_value='Компания «Компетенс Маркетинг»',
                supporting_quote=text,
                accepted=True,
                reason="одна и та же компания",
            )
        ],
        transcript=[
            VerificationTurn(
                seq=1,
                turn_id=TURN_ID,
                speaker="employer",
                text=text,
                asr_confidence=0.84,
                evidence_reference="phonegate-call.wav",
            )
        ],
    )
    assert decision.status is PhoneVerificationStatus.HIGH_CONFIDENCE
    assert decision.facts[0].state is CallFactState.CANDIDATE
    assert decision.facts[0].normalized_value == "Компетенс Маркетинг"


def test_callback_request_can_be_high_confidence_without_interview_facts() -> None:
    text = 'Добрый день. Компания «Компетенс Маркетинг». Перезвоните, пожалуйста.'
    context = VerificationContext(
        call_id="call",
        call_started_at=datetime(2026, 9, 15, 11, tzinfo=UTC),
        timezone="Europe/Chisinau",
        transcript=[
            VerificationTurn(
                seq=1,
                turn_id=TURN_ID,
                speaker="employer",
                text=text,
                asr_confidence=0.84,
                evidence_reference="phonegate-call.wav",
            )
        ],
    )
    decision = reconcile_verification(
        context=context,
        extracted=ExtractionResult(
            summary_text="Работодатель просит перезвонить.",
            outcome_guess="callback_requested",
            facts=[],
            review_reasons=[],
        ),
        verified=VerificationResult(facts=[], review_reasons=[]),
        arbitration=ArbitrationResult(decisions=[]),
        asr_floor=0.7,
        evidence_turn_ids=[TURN_ID],
    )
    assert decision.status is PhoneVerificationStatus.HIGH_CONFIDENCE
    assert decision.facts == ()


def test_callback_request_stays_review_when_asr_is_below_floor() -> None:
    context = VerificationContext(
        call_id="call",
        call_started_at=datetime(2026, 9, 15, 11, tzinfo=UTC),
        timezone="Europe/Chisinau",
        transcript=[
            VerificationTurn(
                seq=1,
                turn_id=TURN_ID,
                speaker="employer",
                text="Перезвоните, пожалуйста.",
                asr_confidence=0.65,
                evidence_reference="phonegate-call.wav",
            )
        ],
    )
    decision = reconcile_verification(
        context=context,
        extracted=ExtractionResult(
            summary_text="Работодатель просит перезвонить.",
            outcome_guess="callback_requested",
            facts=[],
            review_reasons=[],
        ),
        verified=VerificationResult(facts=[], review_reasons=[]),
        arbitration=ArbitrationResult(decisions=[]),
        asr_floor=0.7,
        evidence_turn_ids=[TURN_ID],
    )
    assert decision.status is PhoneVerificationStatus.NEEDS_REVIEW
    assert "follow-up outcome lacks high-confidence transcript evidence" in decision.reasons
