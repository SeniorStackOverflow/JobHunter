from __future__ import annotations

# ruff: noqa: B008
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_actor
from app.database import get_session
from app.models.entities import (
    AuditEvent,
    CallFact,
    CommunicationSession,
    CommunicationTurn,
    PhoneChannelHealth,
    PhoneDeviceSnapshot,
)
from app.models.enums import (
    CommunicationChannel,
    CommunicationOutcome,
    PhoneComponentStatus,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnSpeaker,
)
from app.phone.health import HealthComponent, agent_component_is_stale, channel_status
from app.phone.numbers import mask_phone
from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY
from app.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/phone", tags=["phone"])


@router.get("/status", dependencies=[Depends(require_api_actor)])
async def phone_status(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = list((await session.scalars(select(PhoneChannelHealth))).all())
    agent_row = next((r for r in rows if r.component == "agent"), None)
    agent_stale = agent_row is not None and agent_component_is_stale(
        agent_row.updated_at,
        stale_after_seconds=get_settings().phone_health_stale_after_seconds,
    )

    def _effective_status(row: PhoneChannelHealth) -> PhoneComponentStatus:
        if row.component == "agent" and agent_stale:
            return PhoneComponentStatus.UNAVAILABLE
        return row.status

    components = [
        HealthComponent(r.component, _effective_status(r), r.detail, r.last_ok_at) for r in rows
    ]
    newest = await session.scalar(
        select(CommunicationSession).order_by(desc(CommunicationSession.started_at)).limit(1)
    )

    # Read device snapshot
    device_snapshot = await session.scalar(
        select(PhoneDeviceSnapshot).where(PhoneDeviceSnapshot.id == "current")
    )
    device_block = (
        {**device_snapshot.payload, "updated_at": device_snapshot.updated_at.isoformat()}
        if device_snapshot
        else {}
    )

    # Auto-answer state lives in Redis; the API process has no shared async-redis
    # dependency, so open a short-lived connection (this endpoint is diagnostic, not hot).
    redis = None
    try:
        redis = AsyncRedis.from_url(get_settings().redis_url, decode_responses=True)
        raw_stopped: str | None = await redis.get(AUTO_ANSWER_STOPPED_KEY)
        stopped = raw_stopped == "1"
    except (OSError, RedisError) as exc:
        logger.warning("phone_status_redis_unreachable", error=type(exc).__name__)
        stopped = False
    finally:
        if redis is not None:
            await redis.aclose()

    def _call_block(state: str, sess: CommunicationSession | None) -> dict[str, Any]:
        return {
            "state": state,
            "session_id": str(sess.id) if sess else None,
            "caller_number": mask_phone(sess.remote_address) if sess else None,
            "auto_answered": bool(sess.auto_answered) if sess else False,
            "script_stage": sess.script_stage if sess else None,
        }

    # Determine current_call state
    if newest is None or newest.ended_at is not None:
        current_call = _call_block("idle", None)
    elif newest.answered_at is not None:
        current_call = _call_block("connected", newest)
    else:
        current_call = _call_block("ringing", newest)

    # Spec §9.4: the most recent auto-answer policy decision, as recorded by
    # OrchestratorSupervisor.tick() (audit is the durable record; not mirrored
    # to Redis).
    last_decision_event = await session.scalar(
        select(AuditEvent)
        .where(AuditEvent.action == "communication.auto_answer_decision")
        .order_by(desc(AuditEvent.timestamp))
        .limit(1)
    )
    last_decision = (
        {
            "answer": last_decision_event.sanitized_details.get("answer"),
            "reason": last_decision_event.sanitized_details.get("reason"),
            "at": last_decision_event.timestamp.isoformat(),
        }
        if last_decision_event is not None
        else None
    )

    return {
        "channel": channel_status(components).value if components else "unknown",
        "agent": {
            "last_ok_at": agent_row.last_ok_at.isoformat()
            if agent_row and agent_row.last_ok_at
            else None,
            "status": _effective_status(agent_row).value if agent_row else "unknown",
            "stale": agent_stale,
        },
        "components": [
            {
                "component": r.component,
                "status": _effective_status(r).value,
                "detail": r.detail,
                "last_ok_at": r.last_ok_at.isoformat() if r.last_ok_at else None,
            }
            for r in rows
        ],
        "device": device_block,
        "current_call": current_call,
        "auto_answer": {
            "enabled": get_settings().phone_auto_answer_enabled,
            "stopped": stopped,
            "last_decision": last_decision,
        },
    }


def _safe_summary(summary: dict[str, Any] | None) -> dict[str, Any]:
    """Return list-safe summary data without transcript/SMS evidence payloads."""

    def _clean(value: Any, *, key: str = "") -> Any:
        key_lower = key.casefold()
        if "sms" in key_lower or key_lower in {"transcript", "raw_text"}:
            return None
        if isinstance(value, dict):
            return {
                name: cleaned
                for name, item in value.items()
                if (cleaned := _clean(item, key=str(name))) is not None
            }
        if isinstance(value, list):
            return [_clean(item) for item in value]
        return value

    if not isinstance(summary, dict):
        return {}
    result = {
        key: cleaned
        for key, value in summary.items()
        if key != "verification" and (cleaned := _clean(value, key=str(key))) is not None
    }
    telegram = result.get("telegram")
    if isinstance(telegram, dict):
        result["telegram"] = {"state": telegram.get("state")}
    return result


def _telegram_state(call: CommunicationSession) -> str | None:
    summary = call.summary if isinstance(call.summary, dict) else {}
    telegram = summary.get("telegram")
    return telegram.get("state") if isinstance(telegram, dict) else None


def _session_row(call: CommunicationSession, turn_count: int) -> dict[str, Any]:
    return {
        "id": str(call.id),
        "profile_id": str(call.profile_id),
        "application_id": str(call.application_id) if call.application_id else None,
        "direction": call.direction.value,
        "remote_address": mask_phone(call.remote_address),
        "started_at": call.started_at.isoformat(),
        "answered_at": call.answered_at.isoformat() if call.answered_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "outcome": call.outcome.value if call.outcome else None,
        "needs_review": call.needs_review,
        "turn_count": turn_count,
        "summary": _safe_summary(call.summary),
        "summary_state": call.summary_state.value,
        "verification_status": call.verification_status.value,
        "verification_revision": call.verification_revision,
        "telegram_state": _telegram_state(call),
        "script_stage": call.script_stage,
        "auto_answered": call.auto_answered,
    }


@router.get("/sessions", dependencies=[Depends(require_api_actor)])
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    summary_state: PhoneSummaryState | None = None,
    verification_status: PhoneVerificationStatus | None = None,
    telegram_state: str | None = Query(None, max_length=32),
    needs_review: bool | None = None,
    outcome: CommunicationOutcome | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(CommunicationSession).where(
        CommunicationSession.channel == CommunicationChannel.CALL
    )
    if summary_state is not None:
        stmt = stmt.where(CommunicationSession.summary_state == summary_state)
    if verification_status is not None:
        stmt = stmt.where(CommunicationSession.verification_status == verification_status)
    if needs_review is not None:
        stmt = stmt.where(CommunicationSession.needs_review.is_(needs_review))
    if outcome is not None:
        stmt = stmt.where(CommunicationSession.outcome == outcome)
    candidates = list(
        (await session.scalars(stmt.order_by(desc(CommunicationSession.started_at)))).all()
    )
    if telegram_state is not None:
        candidates = [call for call in candidates if _telegram_state(call) == telegram_state]
    total = len(candidates)
    calls = candidates[offset : offset + limit]
    counts: dict[UUID, int] = {
        session_id: int(count)
        for session_id, count in (
            await session.execute(
                select(CommunicationTurn.session_id, func.count(CommunicationTurn.id))
                .where(CommunicationTurn.session_id.in_([c.id for c in calls] or [None]))
                .group_by(CommunicationTurn.session_id)
            )
        ).all()
    }
    return {
        "sessions": [_session_row(c, int(counts.get(c.id, 0))) for c in calls],
        "total": total,
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < total else None,
    }


@router.get("/sessions/{session_id}", dependencies=[Depends(require_api_actor)])
async def session_detail(
    session_id: UUID, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    call = await session.get(CommunicationSession, session_id)
    if call is None or call.channel is not CommunicationChannel.CALL:
        raise HTTPException(status_code=404, detail="session not found")
    turns = list(
        (
            await session.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.session_id == session_id)
                .order_by(CommunicationTurn.seq)
            )
        ).all()
    )
    facts = list(
        (
            await session.scalars(
                select(CallFact).where(CallFact.session_id == session_id).order_by(CallFact.field)
            )
        ).all()
    )
    turn_by_id = {turn.id: turn for turn in turns}
    verification = (call.summary or {}).get("verification", {})
    if not isinstance(verification, dict):
        verification = {}
    sms_sessions = list(
        (
            await session.scalars(
                select(CommunicationSession)
                .where(
                    CommunicationSession.channel == CommunicationChannel.SMS,
                    CommunicationSession.profile_id == call.profile_id,
                )
                .order_by(desc(CommunicationSession.started_at))
            )
        ).all()
    )
    from app.phone.numbers import normalize_e164

    call_number = normalize_e164(call.remote_address) or call.remote_address
    sms_sessions = [
        sms
        for sms in sms_sessions
        if (normalize_e164(sms.remote_address) or sms.remote_address) == call_number
    ]
    audit_events = list(
        (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.entity_id == str(call.id))
                .order_by(desc(AuditEvent.timestamp))
            )
        ).all()
    )

    async def _sms_row(item: CommunicationSession, *, include_text: bool) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": str(item.id),
            "external_id": item.transport_external_id,
            "remote_address": mask_phone(item.remote_address),
            "started_at": item.started_at.isoformat(),
            "needs_review": item.needs_review,
            "related_session_id": str(item.related_session_id) if item.related_session_id else None,
        }
        if include_text:
            sms_turn = next(
                (
                    turn
                    for turn in (
                        await session.scalars(
                            select(CommunicationTurn).where(CommunicationTurn.session_id == item.id)
                        )
                    ).all()
                    if turn.speaker is TurnSpeaker.EMPLOYER
                ),
                None,
            )
            row["text"] = sms_turn.text if sms_turn is not None else None
        return row

    related_sms = [
        await _sms_row(item, include_text=True)
        for item in sms_sessions
        if item.related_session_id == call.id
    ]
    unlinked_sms = [
        await _sms_row(item, include_text=False)
        for item in sms_sessions
        if item.related_session_id is None
    ]
    pass_metadata = verification.get("pass_metadata", {})
    pass_results = verification.get("pass_results", {})
    pass_decisions: list[dict[str, Any]] = []
    for name in ("extractor", "verifier", "arbiter"):
        value = pass_results.get(name, {}) if isinstance(pass_results, dict) else {}
        metadata = pass_metadata.get(name, {}) if isinstance(pass_metadata, dict) else {}
        pass_decisions.append(
            {
                "pass": name,
                "status": value.get("status") if isinstance(value, dict) else None,
                "review_reasons": (
                    value.get("review_reasons", value.get("reasons", []))
                    if isinstance(value, dict)
                    else []
                ),
                "latency_ms": metadata.get("latency_ms") if isinstance(metadata, dict) else None,
            }
        )
    facts_payload = []
    for fact in facts:
        source_turn = turn_by_id.get(fact.source_turn_id) if fact.source_turn_id else None
        facts_payload.append(
            {
                "id": str(fact.id),
                "field": fact.field,
                "raw_expression": fact.raw_expression,
                "normalized_value": fact.normalized_value,
                "state": fact.state.value,
                "state_label": {
                    "candidate": "Высокая уверенность",
                    "confirmed": "Подтверждено",
                    "conflict": "Нужна проверка: конфликт",
                    "unknown": "Нужна проверка: значение неизвестно",
                }.get(fact.state.value, "Нужна проверка"),
                "confirmation_source": (
                    fact.confirmation_source.value if fact.confirmation_source else None
                ),
                "confirmed_at": fact.confirmed_at.isoformat() if fact.confirmed_at else None,
                "source_quote": source_turn.text if source_turn else None,
                "evidence_url": (
                    f"/admin/phone/evidence/{call.id}/{source_turn.phonegate_transcript_id}.wav"
                    if (
                        source_turn
                        and source_turn.audio_evidence_path
                        and source_turn.phonegate_transcript_id is not None
                    )
                    else None
                ),
            }
        )
    review_reasons = verification.get("review_reason_codes", [])
    if not isinstance(review_reasons, list):
        review_reasons = []
    decision = verification.get("decision")
    if isinstance(decision, dict) and isinstance(decision.get("reasons"), list):
        review_reasons = list(dict.fromkeys([*review_reasons, *decision["reasons"]]))
    return {
        **_session_row(call, len(turns)),
        "diagnostics": call.diagnostics,
        "rx_frame_stats": call.rx_frame_stats,
        "facts": facts_payload,
        "pass_decisions": pass_decisions,
        "review_reasons": review_reasons,
        "related_sms": related_sms,
        "unlinked_sms": unlinked_sms,
        "audit_events": [
            {
                "action": event.action,
                "actor": event.actor,
                "decision": event.decision,
                "at": event.timestamp.isoformat(),
                "details": event.sanitized_details,
            }
            for event in audit_events
        ],
        "verification": verification,
        "turns": [
            {
                "seq": t.seq,
                "speaker": t.speaker.value,
                "text": t.text,
                "asr_backend": t.asr_backend,
                "asr_confidence": t.asr_confidence,
                "occurred_at": t.occurred_at.isoformat(),
                "audio_evidence_url": (
                    f"/admin/phone/evidence/{call.id}/{t.phonegate_transcript_id}.wav"
                    if t.audio_evidence_path and t.phonegate_transcript_id is not None
                    else None
                ),
            }
            for t in turns
        ],
    }
