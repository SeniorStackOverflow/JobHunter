"""Persistence for the current reconciled call facts and verification audit."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import CallFact, CommunicationSession
from app.models.enums import PhoneVerificationStatus
from app.phone.reconciliation import VerificationDecision
from app.phone.verification import ModelCallMeta


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
            }
            for fact in decision.facts
        ],
        "reasons": list(decision.reasons),
    }


def _result_json(result: BaseModel) -> dict[str, Any]:
    return result.model_dump(mode="json")


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
        "stored_at": datetime.now(UTC).isoformat(),
    }
    current["verification"] = verification
    call.summary = current
    call.verification_status = decision.status
    call.needs_review = decision.status is not PhoneVerificationStatus.HIGH_CONFIDENCE

    existing = {
        fact.field: fact
        for fact in (
            await db.execute(select(CallFact).where(CallFact.session_id == call.id))
        ).scalars()
    }
    for reconciled in decision.facts:
        fact = existing.get(reconciled.field)
        if fact is not None and fact.confirmation_source is not None:
            continue
        if fact is None:
            fact = CallFact(session_id=call.id, field=reconciled.field)
            db.add(fact)
        fact.source_turn_id = reconciled.source_turn_id
        fact.raw_expression = reconciled.raw_expression
        fact.normalized_value = reconciled.normalized_value
        fact.asr_confidence = reconciled.asr_confidence
        fact.llm_confidence = reconciled.llm_confidence
        fact.state = reconciled.state


__all__ = ["replace_current_facts"]
