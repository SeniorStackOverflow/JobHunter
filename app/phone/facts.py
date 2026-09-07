"""Persistence for the current reconciled call facts and verification audit."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.entities import CallFact, CommunicationSession, CommunicationTurn
from app.models.enums import (
    CallFactConfirmationSource,
    CallFactState,
    CommunicationChannel,
    CommunicationDirection,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnSpeaker,
)
from app.phone.critical import (
    CriticalField,
    canonical_critical_value,
    normalize_critical_value,
    sms_field_evidence_matches,
)
from app.phone.notification_state import refresh_telegram_notification
from app.phone.reconciliation import VerificationDecision
from app.phone.verification import ModelCallMeta, SmsComparisonResult

_SMS_PIPELINE_VERSION = "phone-2b-sms-v1"
_MAX_SAFE_SMS_TEXT = 500


class SmsConfirmationRejected(ValueError):
    """The supplied SMS is not an eligible employer message for this call."""


class CallMutationRejected(RuntimeError):
    """A manual trust mutation lost its revision/lease compare-and-swap."""


async def reserve_call_mutation(
    db: AsyncSession,
    *,
    call: CommunicationSession,
) -> CommunicationSession:
    """Reserve a call trust mutation with a row lock or SQLite CAS.

    PostgreSQL serializes this boundary with ``FOR UPDATE``.  SQLite has no
    row locks, so it conditionally advances the observed revision while the
    claim is still null.  Callers must keep all related writes in this same
    transaction; rollback then removes both the reservation and any facts.
    """
    await db.flush()
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect != "postgresql":
        if call.claim_token is not None:
            raise CallMutationRejected("call is unavailable for mutation")
        observed_revision = call.verification_revision
        result = await db.execute(
            update(CommunicationSession)
            .where(
                CommunicationSession.id == call.id,
                CommunicationSession.verification_revision == observed_revision,
                CommunicationSession.claim_token.is_(None),
            )
            .values(verification_revision=observed_revision + 1)
        )
        if int(getattr(result, "rowcount", 0)) != 1:
            raise CallMutationRejected("call changed during mutation")
        await db.refresh(call)
        return call

    query = (
        select(CommunicationSession)
        .where(CommunicationSession.id == call.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    latest = await db.scalar(query)
    if latest is None or latest.claim_token is not None:
        raise CallMutationRejected("call is unavailable for mutation")
    else:
        observed_revision = latest.verification_revision
        latest.verification_revision = observed_revision + 1
    return latest


def _meta_json(metadata: ModelCallMeta) -> dict[str, object]:
    return {
        "provider": metadata.provider,
        "model": metadata.model,
        "latency_ms": metadata.latency_ms,
        "attempts": metadata.attempts,
    }


def _decision_json(decision: VerificationDecision) -> dict[str, object]:
    return {
        "status": decision.status.value,
        "facts": [
            {
                "field": fact.field,
                "raw_expression": fact.raw_expression,
                "normalized_value": fact.normalized_value,
                "source_turn_id": str(fact.source_turn_id) if fact.source_turn_id else None,
                "asr_confidence": fact.asr_confidence,
                "llm_confidence": fact.llm_confidence,
                "state": fact.state.value,
                "reason": fact.reason,
                "supporting_quote": fact.supporting_quote,
            }
            for fact in decision.facts
        ],
        "reasons": list(decision.reasons),
    }


def _result_json(result: BaseModel) -> dict[str, Any]:
    return result.model_dump(mode="json")


def _safe_sms_text(value: str) -> str:
    return " ".join(value.split())[:_MAX_SAFE_SMS_TEXT]


def _safe_sms_metadata(metadata: ModelCallMeta) -> dict[str, object]:
    return {
        "provider": _safe_sms_text(metadata.provider),
        "model": _safe_sms_text(metadata.model),
        "latency_ms": max(0, int(metadata.latency_ms)),
        "attempts": max(1, int(metadata.attempts)),
    }


def _verification_summary(call: CommunicationSession) -> dict[str, Any]:
    summary = dict(call.summary or {})
    verification = summary.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    summary["verification"] = verification
    call.summary = summary
    return verification


def _sms_turn_id_set(verification: Mapping[str, Any]) -> set[str]:
    values = verification.get("sms_input_ids", [])
    if not isinstance(values, list):
        return set()
    return {value for value in values if isinstance(value, str)}


def _safe_comparison_json(
    comparison: SmsComparisonResult, *, valid_fields: set[CriticalField]
) -> list[dict[str, str]]:
    return [
        {
            "field": item.field,
            "relation": item.relation,
            "reason_code": (
                "accepted"
                if item.field in valid_fields and item.relation in {"matches", "conflicts"}
                else "review_required"
            ),
        }
        for item in comparison.comparisons
    ]


async def _validate_sms_confirmation(
    db: AsyncSession,
    *,
    call: CommunicationSession,
    sms_session: CommunicationSession,
) -> CommunicationTurn:
    if call.channel is not CommunicationChannel.CALL:
        raise SmsConfirmationRejected("call is not a call session")
    if sms_session.channel is not CommunicationChannel.SMS:
        raise SmsConfirmationRejected("source is not an SMS session")
    if sms_session.direction is not CommunicationDirection.INBOUND:
        raise SmsConfirmationRejected("SMS is not inbound")
    if sms_session.profile_id != call.profile_id:
        raise SmsConfirmationRejected("SMS identity does not match call")
    if sms_session.related_session_id != call.id:
        raise SmsConfirmationRejected("SMS is not linked to this call")
    if sms_session.transport != "phonegate" or not sms_session.transport_external_id:
        raise SmsConfirmationRejected("SMS external identity is invalid")
    turns = list(
        (
            await db.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.session_id == sms_session.id)
                .order_by(CommunicationTurn.seq, CommunicationTurn.id)
            )
        ).all()
    )
    if len(turns) != 1:
        raise SmsConfirmationRejected("SMS must have one employer turn")
    turn = turns[0]
    if turn.seq != 1 or turn.speaker is not TurnSpeaker.EMPLOYER:
        raise SmsConfirmationRejected("SMS employer turn is invalid")
    return turn


def _fact_snapshot(fact: CallFact) -> dict[str, object]:
    return {
        "state": fact.state.value,
        "raw_expression": fact.raw_expression,
        "normalized_value": fact.normalized_value,
        "source_turn_id": str(fact.source_turn_id) if fact.source_turn_id else None,
        "asr_confidence": fact.asr_confidence,
        "llm_confidence": fact.llm_confidence,
    }


def _fact_trust_state(facts: Sequence[CallFact]) -> dict[str, dict[str, object]]:
    return {
        fact.field: {
            **_fact_snapshot(fact),
            "confirmation_source": (
                fact.confirmation_source.value if fact.confirmation_source is not None else None
            ),
            "confirmed_by_turn_id": (
                str(fact.confirmed_by_turn_id) if fact.confirmed_by_turn_id else None
            ),
            "confirmed_at": fact.confirmed_at.isoformat() if fact.confirmed_at else None,
        }
        for fact in sorted(facts, key=lambda item: item.field)
    }


def _comparison_input(comparison: SmsComparisonResult) -> list[dict[str, str]]:
    return sorted(
        ({"field": item.field, "relation": item.relation} for item in comparison.comparisons),
        key=lambda item: (item["field"], item["relation"]),
    )


def _supporter_state(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(field): sorted(str(item) for item in values if isinstance(item, str))
        for field, values in value.items()
        if isinstance(values, list)
    }


def _sms_replay_is_equivalent(
    *,
    existing_entry: dict[str, Any],
    comparison: SmsComparisonResult,
    metadata: ModelCallMeta,
    facts: Sequence[CallFact],
    supporters: object,
    status: PhoneVerificationStatus,
) -> bool:
    if existing_entry.get("status") == "unlinked":
        return False
    if existing_entry.get("metadata") != _safe_sms_metadata(metadata):
        return False
    if existing_entry.get("comparison_input") != _comparison_input(comparison):
        return False
    if existing_entry.get("fact_state") != _fact_trust_state(facts):
        return False
    if _supporter_state(existing_entry.get("supporters")) != _supporter_state(supporters):
        return False
    return existing_entry.get("status") == status.value


def _restore_fact_snapshot(fact: CallFact, snapshot: Mapping[str, object]) -> None:
    state = snapshot.get("state")
    if state not in {item.value for item in CallFactState}:
        return
    fact.state = CallFactState(state)
    raw = snapshot.get("raw_expression")
    normalized = snapshot.get("normalized_value")
    source_turn_id = snapshot.get("source_turn_id")
    fact.raw_expression = raw if isinstance(raw, str) else fact.raw_expression
    fact.normalized_value = normalized if isinstance(normalized, str) else None
    fact.source_turn_id = UUID(source_turn_id) if isinstance(source_turn_id, str) else None
    asr_confidence = snapshot.get("asr_confidence")
    fact.asr_confidence = (
        float(asr_confidence) if isinstance(asr_confidence, (int, float)) else None
    )
    llm_confidence = snapshot.get("llm_confidence")
    fact.llm_confidence = (
        float(llm_confidence) if isinstance(llm_confidence, (int, float)) else None
    )
    fact.confirmed_by_turn_id = None
    fact.confirmation_source = None
    fact.confirmed_at = None


def derive_verification_status(
    call: CommunicationSession,
    facts: Sequence[CallFact],
    *,
    review: bool,
    verification: Mapping[str, Any] | None = None,
) -> PhoneVerificationStatus:
    facts_list = list(facts)
    if review or any(
        fact.state in {CallFactState.CONFLICT, CallFactState.UNKNOWN} for fact in facts_list
    ):
        return PhoneVerificationStatus.NEEDS_REVIEW
    verification_data = verification or {}
    hints = call.summary.get("hints", {}) if isinstance(call.summary, dict) else {}
    scheduled = bool(
        any(fact.field in {"interview_date", "interview_time"} for fact in facts_list)
        or (isinstance(hints, dict) and hints.get("outcome_guess") == "interview_proposed")
    )
    if scheduled:
        required = {"interview_date", "interview_time"}
        by_field = {fact.field: fact for fact in facts_list}
        if any(
            by_field.get(field) is None
            or by_field[field].state not in {CallFactState.CANDIDATE, CallFactState.CONFIRMED}
            for field in required
        ):
            return PhoneVerificationStatus.NEEDS_REVIEW
    confirmation_fields = {
        "interview_date",
        "interview_time",
        "timezone",
        "format",
        "address",
        "meeting_url",
    }
    mentioned_confirmation_facts = [
        fact for fact in facts_list if fact.field in confirmation_fields
    ]
    association_facts = [fact for fact in facts_list if fact.field in {"company", "vacancy"}]
    if (
        mentioned_confirmation_facts
        and all(fact.state is CallFactState.CONFIRMED for fact in mentioned_confirmation_facts)
        and all(
            fact.state in {CallFactState.CANDIDATE, CallFactState.CONFIRMED}
            for fact in association_facts
        )
    ):
        return PhoneVerificationStatus.CONFIRMED
    stored = verification_data.get("decision", {})
    if isinstance(stored, dict):
        status = stored.get("status")
        if status in {item.value for item in PhoneVerificationStatus}:
            return PhoneVerificationStatus(status)
    prior = verification_data.get("transcript_status")
    if prior in {item.value for item in PhoneVerificationStatus}:
        return PhoneVerificationStatus(prior)
    return call.verification_status


_sms_status = derive_verification_status


def _folded(value: str) -> str:
    import unicodedata

    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


async def apply_sms_confirmation(
    db: AsyncSession,
    *,
    call: CommunicationSession,
    sms_session: CommunicationSession,
    comparison: SmsComparisonResult,
    metadata: ModelCallMeta,
) -> PhoneVerificationStatus:
    """Apply one typed SMS comparison to a linked call, conservatively.

    Semantic model output is only an eligibility signal. A matching comparison
    is accepted only when the deterministic parser produces the same canonical
    value as the persisted call fact.
    """
    turn = await _validate_sms_confirmation(db, call=call, sms_session=sms_session)
    verification = _verification_summary(call)
    if "transcript_status" not in verification:
        verification["transcript_status"] = call.verification_status.value
    comparisons_history = verification.get("sms_comparisons", [])
    if not isinstance(comparisons_history, list):
        comparisons_history = []
    turn_key = str(turn.id)
    existing_entry = next(
        (
            item
            for item in comparisons_history
            if isinstance(item, dict) and item.get("sms_turn_id") == turn_key
        ),
        None,
    )
    facts = list((await db.scalars(select(CallFact).where(CallFact.session_id == call.id))).all())
    supporters = verification.get("sms_supporters", {})
    if not isinstance(supporters, dict):
        supporters = {}
    if existing_entry is not None and _sms_replay_is_equivalent(
        existing_entry=existing_entry,
        comparison=comparison,
        metadata=metadata,
        facts=facts,
        supporters=supporters,
        status=call.verification_status,
    ):
        return call.verification_status
    by_field = {cast(CriticalField, fact.field): fact for fact in facts}
    reasons: list[str] = []
    matched_fields: set[CriticalField] = set()
    previous: dict[str, dict[str, object]] = {}
    valid_fields: set[CriticalField] = set()
    sms_text = _folded(turn.text)
    originals = verification.get("sms_originals", {})
    if not isinstance(originals, dict):
        originals = {}
    for item in comparison.comparisons:
        field = item.field
        fact = by_field.get(field)
        if item.relation == "not_mentioned":
            continue
        if item.relation == "ambiguous":
            reasons.append(f"sms:{field}:ambiguous")
            continue
        if fact is None:
            reasons.append(f"sms:{field}:fact_missing")
            continue
        if item.relation in {"matches", "conflicts"}:
            expression = _folded(item.sms_expression)
            if not expression or expression not in sms_text:
                reasons.append(f"sms:{field}:expression_unbound")
                continue
            if not sms_field_evidence_matches(
                field,
                turn.text,
                item.sms_expression,
                reference_at=turn.occurred_at,
                timezone="Europe/Chisinau",
            ):
                reasons.append(f"sms:{field}:full_turn_ambiguous")
                continue
            if item.call_expression and _folded(item.call_expression) not in _folded(
                fact.raw_expression
            ):
                reasons.append(f"sms:{field}:call_expression_unbound")
                continue
        if field not in matched_fields:
            previous[field] = _fact_snapshot(fact)
            matched_fields.add(field)
        if field not in originals:
            originals[field] = _fact_snapshot(fact)
        if item.relation == "conflicts":
            fact.state = CallFactState.CONFLICT
            reasons.append(f"sms:{field}:conflict")
            valid_fields.add(field)
            continue
        sms_value = normalize_critical_value(
            field,
            item.sms_expression,
            reference_at=turn.occurred_at,
            timezone="Europe/Chisinau",
        )
        call_value = canonical_critical_value(field, fact.normalized_value)
        sms_canonical = canonical_critical_value(field, sms_value)
        if sms_canonical is None or call_value is None or sms_canonical != call_value:
            fact.state = CallFactState.CONFLICT
            reasons.append(f"sms:{field}:deterministic_mismatch")
            valid_fields.add(field)
            continue
        if fact.state is CallFactState.CONFLICT:
            reasons.append(f"sms:{field}:conflict_sticky")
            valid_fields.add(field)
            continue
        field_supporters = supporters.get(field, [])
        if not isinstance(field_supporters, list):
            field_supporters = []
        if turn_key not in field_supporters:
            field_supporters = [*field_supporters, turn_key]
        supporters[field] = field_supporters
        if fact.confirmation_source is not None:
            # Preserve the first trusted confirmer and its audit identity.
            valid_fields.add(field)
            continue
        fact.state = CallFactState.CONFIRMED
        fact.confirmed_by_turn_id = turn.id
        fact.confirmation_source = CallFactConfirmationSource.SMS
        fact.confirmed_at = datetime.now(UTC)
        valid_fields.add(field)

    review = bool(reasons) or any(fact.state is CallFactState.CONFLICT for fact in facts)
    status = derive_verification_status(call, facts, review=review, verification=verification)
    now = datetime.now(UTC).isoformat()
    entry: dict[str, Any] = {
        "sms_turn_id": turn_key,
        "source": CallFactConfirmationSource.SMS.value,
        "confirmed_at": now,
        "status": status.value,
        "pipeline_version": verification.get("pipeline_version", _SMS_PIPELINE_VERSION),
        "metadata": _safe_sms_metadata(metadata),
        "comparison_input": _comparison_input(comparison),
        "comparisons": _safe_comparison_json(comparison, valid_fields=valid_fields),
        "reason_codes": reasons,
        "previous": previous,
        "supporters": {field: list(value) for field, value in supporters.items()},
        "fact_state": _fact_trust_state(facts),
    }
    if existing_entry is not None:
        comparisons_history = [item for item in comparisons_history if item is not existing_entry]
    comparisons_history.append(entry)
    verification["sms_comparisons"] = comparisons_history
    input_ids = _sms_turn_id_set(verification)
    # Applying a comparison persists trust/fact state even when a retry refers
    # to an already linked SMS. Bind every such mutation to a fresh revision.
    call.verification_revision += 1
    input_ids.add(turn_key)
    verification["sms_input_ids"] = sorted(input_ids)
    if reasons:
        prior_reasons = verification.get("review_reason_codes", [])
        if not isinstance(prior_reasons, list):
            prior_reasons = []
        verification["review_reason_codes"] = list(dict.fromkeys([*prior_reasons, *reasons]))
    verification["sms_supporters"] = supporters
    verification["sms_originals"] = originals
    summary = dict(call.summary or {})
    summary["verification"] = verification
    call.summary = summary
    flag_modified(call, "summary")
    refresh_telegram_notification(call)
    flag_modified(call, "summary")
    call.verification_status = status
    call.needs_review = status is PhoneVerificationStatus.NEEDS_REVIEW
    return status


async def unlink_sms_confirmation(
    db: AsyncSession,
    *,
    call: CommunicationSession,
    sms_session: CommunicationSession,
) -> PhoneVerificationStatus:
    """Detach one SMS and reverse only facts solely confirmed by its turn."""
    await db.flush()
    call_query = (
        select(CommunicationSession)
        .where(CommunicationSession.id == call.id)
        .execution_options(populate_existing=True)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        call_query = call_query.with_for_update()
    latest_call = await db.scalar(call_query)
    sms_query = (
        select(CommunicationSession)
        .where(CommunicationSession.id == sms_session.id)
        .execution_options(populate_existing=True)
    )
    latest_sms = await db.scalar(sms_query)
    if latest_call is None or latest_sms is None:
        raise SmsConfirmationRejected("call or SMS no longer exists")
    if latest_call.claim_token is not None:
        raise SmsConfirmationRejected("call has an active processing claim")
    if db.bind is not None and db.bind.dialect.name != "postgresql":
        result = await db.execute(
            update(CommunicationSession)
            .where(
                CommunicationSession.id == latest_call.id,
                CommunicationSession.verification_revision == latest_call.verification_revision,
                CommunicationSession.claim_token.is_(None),
            )
            .values(verification_revision=latest_call.verification_revision)
        )
        if int(getattr(result, "rowcount", 0)) != 1:
            raise SmsConfirmationRejected("call changed during unlink")
    call = latest_call
    sms_session = latest_sms
    turn = await _validate_sms_confirmation(db, call=call, sms_session=sms_session)
    verification = _verification_summary(call)
    entries = verification.get("sms_comparisons", [])
    if not isinstance(entries, list):
        entries = []
    entry = next(
        (
            item
            for item in entries
            if isinstance(item, dict) and item.get("sms_turn_id") == str(turn.id)
        ),
        None,
    )
    facts = list((await db.scalars(select(CallFact).where(CallFact.session_id == call.id))).all())
    if isinstance(entry, dict):
        supporters = verification.get("sms_supporters", {})
        if not isinstance(supporters, dict):
            supporters = {}
        originals = verification.get("sms_originals", {})
        if not isinstance(originals, dict):
            originals = {}
        for fact in facts:
            field_supporters = supporters.get(fact.field, [])
            if not isinstance(field_supporters, list):
                field_supporters = []
            if str(turn.id) in field_supporters:
                field_supporters = [value for value in field_supporters if value != str(turn.id)]
                supporters[fact.field] = field_supporters
            if fact.state is CallFactState.CONFLICT:
                continue
            if field_supporters:
                if fact.confirmation_source is CallFactConfirmationSource.SMS:
                    fact.confirmed_by_turn_id = UUID(field_supporters[0])
                continue
            if (
                fact.confirmation_source is CallFactConfirmationSource.SMS
                and fact.confirmed_by_turn_id == turn.id
            ):
                snapshot = originals.get(fact.field)
                if isinstance(snapshot, dict):
                    _restore_fact_snapshot(fact, snapshot)
        verification["sms_supporters"] = supporters
        entry["status"] = "unlinked"
        entry["unlinked_at"] = datetime.now(UTC).isoformat()
    sms_session.related_session_id = None
    for key in ("sms_pending", "sms_input_ids"):
        values = verification.get(key, [])
        if isinstance(values, list):
            verification[key] = [value for value in values if value != str(turn.id)]
    attempts = verification.get("sms_attempts", {})
    if isinstance(attempts, dict):
        attempts.pop(str(turn.id), None)
        verification["sms_attempts"] = attempts
    call.verification_revision += 1
    verification["sms_reconciliation"] = {
        "state": "unlinked",
        "sms_turn_id": str(turn.id),
        "reason": "manual_unlink",
    }
    if call.auto_answered and call.ended_at is not None:
        call.summary_state = PhoneSummaryState.PENDING
        call.processing_started_at = None
    status = derive_verification_status(call, facts, review=False, verification=verification)
    call.verification_status = status
    call.needs_review = status is PhoneVerificationStatus.NEEDS_REVIEW
    summary = dict(call.summary or {})
    summary["verification"] = verification
    call.summary = summary
    flag_modified(call, "summary")
    refresh_telegram_notification(call)
    flag_modified(call, "summary")
    return status


async def replace_current_facts(
    db: AsyncSession,
    *,
    call: CommunicationSession,
    decision: VerificationDecision,
    pass_metadata: Mapping[str, ModelCallMeta],
    input_fingerprint: str,
    pipeline_version: str,
    pass_results: Mapping[str, BaseModel],
) -> None:
    """Upsert model-derived facts and append an audit entry for new source input.

    Confirmation fields are deliberately untouched for existing SMS/manual facts;
    a model retry cannot revoke an explicit human or SMS confirmation.
    """
    current = dict(call.summary or {})
    old_verification = current.get("verification")
    if not isinstance(old_verification, dict):
        old_verification = {}
    old_fingerprint = old_verification.get("input_fingerprint")
    old_pipeline_version = old_verification.get("pipeline_version")
    if old_fingerprint == input_fingerprint and old_pipeline_version == pipeline_version:
        return
    changed_input = old_fingerprint != input_fingerprint or old_pipeline_version != pipeline_version
    if changed_input:
        call.verification_revision += 1
    history = list(old_verification.get("history", []))
    if changed_input and old_fingerprint is not None:
        history.append(
            {
                "revision": call.verification_revision - 1,
                "input_fingerprint": old_fingerprint,
                "pipeline_version": old_verification.get("pipeline_version"),
                "decision": old_verification.get("decision", {}),
                "pass_metadata": old_verification.get("pass_metadata", {}),
                "pass_results": old_verification.get("pass_results", {}),
            }
        )
    verification: dict[str, Any] = {
        "revision": call.verification_revision,
        "input_fingerprint": input_fingerprint,
        "pipeline_version": pipeline_version,
        "decision": _decision_json(decision),
        "pass_metadata": {name: _meta_json(meta) for name, meta in pass_metadata.items()},
        "pass_results": {name: _result_json(result) for name, result in pass_results.items()},
        "history": history,
        "attempt_history": list(old_verification.get("attempt_history", [])),
        "stored_at": datetime.now(UTC).isoformat(),
    }
    for key in (
        "sms_comparisons",
        "sms_input_ids",
        "sms_pending",
        "sms_attempts",
        "sms_supporters",
        "sms_originals",
        "sms_reconciliation",
        "transcript_status",
        "review_reason_codes",
    ):
        if key in old_verification:
            verification[key] = old_verification[key]
    current["verification"] = verification
    call.summary = current
    existing = {
        fact.field: fact
        for fact in (
            await db.execute(select(CallFact).where(CallFact.session_id == call.id))
        ).scalars()
    }
    for reconciled in decision.facts:
        fact = existing.get(reconciled.field)
        if fact is not None and (
            fact.confirmation_source is not None or fact.state is CallFactState.CONFLICT
        ):
            continue
        if fact is None:
            fact = CallFact(session_id=call.id, field=reconciled.field)
            db.add(fact)
            existing[reconciled.field] = fact
        fact.source_turn_id = reconciled.source_turn_id
        fact.raw_expression = reconciled.raw_expression
        fact.normalized_value = reconciled.normalized_value
        fact.asr_confidence = reconciled.asr_confidence
        fact.llm_confidence = reconciled.llm_confidence
        fact.state = reconciled.state

    call.verification_status = derive_verification_status(
        call,
        list(existing.values()),
        review=decision.status is PhoneVerificationStatus.NEEDS_REVIEW,
        verification=verification,
    )
    call.needs_review = call.verification_status is PhoneVerificationStatus.NEEDS_REVIEW
    if changed_input:
        refresh_telegram_notification(call)
        flag_modified(call, "summary")


__all__ = [
    "CallMutationRejected",
    "SmsConfirmationRejected",
    "apply_sms_confirmation",
    "derive_verification_status",
    "replace_current_facts",
    "reserve_call_mutation",
    "unlink_sms_confirmation",
]
