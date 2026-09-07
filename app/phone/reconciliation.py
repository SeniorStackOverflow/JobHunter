"""Conservative reconciliation of independent post-call verification passes."""

# ruff: noqa: RUF001 — Russian correction markers are intentional.

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from uuid import UUID

from app.models.enums import CallFactState, PhoneVerificationStatus
from app.phone.critical import CriticalField, normalize_critical_value
from app.phone.verification import (
    ArbitrationResult,
    ExtractionResult,
    FactCandidate,
    VerificationContext,
    VerificationResult,
)


@dataclass(frozen=True)
class ReconciledFact:
    field: CriticalField
    raw_expression: str
    normalized_value: str | None
    source_turn_id: UUID | None
    asr_confidence: float | None
    llm_confidence: float | None
    state: CallFactState
    reason: str
    supporting_quote: str = ""


@dataclass(frozen=True)
class VerificationDecision:
    status: PhoneVerificationStatus
    facts: tuple[ReconciledFact, ...]
    reasons: tuple[str, ...]


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _comparison_value(field: CriticalField, value: str | None) -> str | None:
    if value is None:
        return None
    value = _normalized_text(value)
    return value.casefold() if field == "address" else value


def _correction_text(text: str) -> bool:
    folded = _normalized_text(text).casefold()
    return bool(
        re.search(
            r"\b(?:точнее|исправлен|исправим|перенес|перенесём|перенесем|вместо|давайте тогда)\b",
            folded,
        )
        or re.search(r"\bне\b.+\bа\b", folded)
    )


@dataclass(frozen=True)
class _CandidateEvidence:
    candidate: FactCandidate
    deterministic_value: str | None
    source_turn_id: UUID | None
    turn_seq: int
    asr_confidence: float | None
    supported: bool
    reasons: tuple[str, ...]
    pass_name: str


def _inspect_candidate(
    *,
    candidate: FactCandidate,
    pass_name: str,
    context: VerificationContext,
    asr_floor: float,
    evidence_turn_ids: set[UUID],
) -> _CandidateEvidence:
    reasons: list[str] = []
    deterministic = normalize_critical_value(
        candidate.field,
        candidate.raw_expression,
        reference_at=context.call_started_at,
        timezone=context.timezone,
    )
    if _comparison_value(candidate.field, candidate.normalized_value) != _comparison_value(
        candidate.field, deterministic
    ):
        reasons.append("normalized value mismatch")
    if candidate.ambiguity.strip():
        reasons.append(f"ambiguity: {candidate.ambiguity.strip()}")
    turn = next((item for item in context.transcript if item.seq == candidate.turn_seq), None)
    source_id: UUID | None = None
    turn_seq = candidate.turn_seq or 0
    asr_confidence: float | None = None
    if turn is None or turn.speaker.casefold() != "employer":
        reasons.append("source turn unresolved")
    else:
        asr_confidence = turn.asr_confidence
        source_id = turn.turn_id
        if _normalized_text(candidate.quote) not in _normalized_text(turn.text):
            reasons.append("quote not found")
        if source_id is None or source_id not in evidence_turn_ids:
            reasons.append("evidence missing")
        if asr_confidence is not None and asr_confidence < asr_floor:
            reasons.append("asr confidence below floor")
    if deterministic is None:
        reasons.append("value is unknown or ambiguous")
    return _CandidateEvidence(
        candidate=candidate,
        deterministic_value=deterministic,
        source_turn_id=source_id,
        turn_seq=turn_seq,
        asr_confidence=asr_confidence,
        supported=not reasons,
        reasons=tuple(reasons),
        pass_name=pass_name,
    )


def _all_candidates(
    extracted: ExtractionResult, verified: VerificationResult
) -> Iterable[tuple[str, FactCandidate]]:
    yield from (("extractor", candidate) for candidate in extracted.facts)
    yield from (("verifier", candidate) for candidate in verified.facts)


def reconcile_verification(
    *,
    context: VerificationContext,
    extracted: ExtractionResult,
    verified: VerificationResult,
    arbitration: ArbitrationResult,
    asr_floor: float,
    evidence_turn_ids: Collection[UUID],
) -> VerificationDecision:
    """Reconcile model candidates while requiring transcript and audio evidence."""
    evidence = set(evidence_turn_ids)
    inspected = [
        _inspect_candidate(
            candidate=candidate,
            pass_name=pass_name,
            context=context,
            asr_floor=asr_floor,
            evidence_turn_ids=evidence,
        )
        for pass_name, candidate in _all_candidates(extracted, verified)
    ]
    by_field: dict[CriticalField, list[_CandidateEvidence]] = defaultdict(list)
    for item in inspected:
        by_field[item.candidate.field].append(item)
    arbitration_by_field = {item.field: item for item in arbitration.decisions}
    facts: list[ReconciledFact] = []
    reasons: list[str] = [*extracted.review_reasons, *verified.review_reasons]
    pass_review_required = bool(extracted.review_reasons or verified.review_reasons)

    for field, candidates in by_field.items():
        supported = [item for item in candidates if item.supported]
        value_candidates = [
            item
            for item in candidates
            if "normalized value mismatch" not in item.reasons
            and "value is unknown or ambiguous" not in item.reasons
        ]
        groups: dict[str | None, list[_CandidateEvidence]] = defaultdict(list)
        for item in value_candidates:
            groups[_comparison_value(field, item.deterministic_value)].append(item)
        arbiter = arbitration_by_field.get(field)
        field_reasons: list[str] = []
        if arbiter is None:
            field_reasons.append("arbiter decision missing")
        elif not arbiter.accepted:
            field_reasons.append(f"arbiter rejected: {arbiter.reason}")
        accepted_group: list[_CandidateEvidence] = []
        correction_applied = False
        if len(groups) == 1 and groups:
            accepted_group = next(iter(groups.values()))
        elif len(groups) > 1:
            latest = max(
                (item for items in groups.values() for item in items),
                key=lambda item: item.turn_seq,
            )
            latest_group = groups[_comparison_value(field, latest.deterministic_value)]
            source_turn = next((t for t in context.transcript if t.seq == latest.turn_seq), None)
            if source_turn is not None and _correction_text(source_turn.text):
                accepted_group = latest_group
                correction_applied = True
                for value, group in groups.items():
                    if group is not latest_group:
                        reasons.extend(
                            f"prior expression {item.candidate.raw_expression}={value} retained"
                            for item in group
                        )
            else:
                field_reasons.append("differing normalized values")
        if len({item.pass_name for item in accepted_group}) < 2:
            field_reasons.append("independent agreement missing")
        if arbiter is not None and accepted_group:
            expected = _comparison_value(field, accepted_group[0].deterministic_value)
            actual = _comparison_value(field, arbiter.accepted_value)
            if not arbiter.accepted or actual != expected:
                field_reasons.append("arbiter value mismatch")
            arbiter_quote = _normalized_text(arbiter.supporting_quote)
            selected = max(accepted_group, key=lambda item: item.turn_seq)
            selected_turn = next(
                (turn for turn in context.transcript if turn.seq == selected.turn_seq),
                None,
            )
            if (
                not arbiter_quote
                or selected_turn is None
                or arbiter_quote not in _normalized_text(selected_turn.text)
            ):
                field_reasons.append("arbiter quote not found")
        if correction_applied:
            field_reasons.extend(reason for item in accepted_group for reason in item.reasons)
            reasons.extend(
                f"superseded expression {item.candidate.raw_expression}: {reason}"
                for item in candidates
                if item not in accepted_group
                for reason in item.reasons
            )
        elif not supported or any(item.reasons for item in candidates if item not in supported):
            field_reasons.extend(reason for item in candidates for reason in item.reasons)
        if not accepted_group or field_reasons:
            state = (
                CallFactState.CONFLICT
                if any(
                    marker in " ".join(field_reasons)
                    for marker in (
                        "differing normalized",
                        "arbiter rejected",
                        "arbiter value mismatch",
                    )
                )
                else CallFactState.UNKNOWN
            )
            chosen = max(candidates, key=lambda item: item.turn_seq)
        else:
            state = CallFactState.CANDIDATE
            chosen = max(accepted_group, key=lambda item: item.turn_seq)
        reason_parts = [*field_reasons]
        if chosen.reasons:
            reason_parts.extend(chosen.reasons)
        reason = "; ".join(dict.fromkeys(reason_parts)) or (
            "independent agreement with transcript evidence"
        )
        fact = ReconciledFact(
            field=field,
            raw_expression=chosen.candidate.raw_expression,
            normalized_value=chosen.deterministic_value,
            source_turn_id=chosen.source_turn_id,
            asr_confidence=chosen.asr_confidence,
            llm_confidence=chosen.candidate.confidence,
            state=state,
            reason=reason,
            supporting_quote=_normalized_text(chosen.candidate.quote),
        )
        facts.append(fact)
        if state is not CallFactState.CANDIDATE:
            reasons.append(f"{field}: {reason}")
        elif field_reasons:
            reasons.extend(field_reasons)

    status = (
        PhoneVerificationStatus.HIGH_CONFIDENCE
        if facts
        and not pass_review_required
        and all(fact.state is CallFactState.CANDIDATE for fact in facts)
        else PhoneVerificationStatus.NEEDS_REVIEW
    )
    return VerificationDecision(
        status=status, facts=tuple(facts), reasons=tuple(dict.fromkeys(reasons))
    )


__all__ = ["ReconciledFact", "VerificationDecision", "reconcile_verification"]
