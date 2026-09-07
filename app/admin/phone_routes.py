from __future__ import annotations

# FastAPI's declarative dependency/form parameters intentionally call Depends/Form.
# ruff: noqa: B008, RUF001
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql.selectable import Select

# ``routes`` is imported as a module (not ``from ... import _phone_redis``) on
# purpose: ``phone_health_context`` and the POST handlers read
# ``_admin_routes._phone_redis`` / ``_admin_routes._audit_admin`` through the
# module object so tests can ``monkeypatch.setattr(admin_routes, "_phone_redis", ...)``
# and have the override take effect here.
from app.admin import routes as _admin_routes
from app.admin.routes import require_admin, require_csrf
from app.database import get_session
from app.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])


async def phone_health_context(session: AsyncSession) -> dict[str, Any]:
    """Query phone channel health and aggregate component status."""
    from app.models.entities import PhoneChannelHealth, PhoneDeviceSnapshot
    from app.models.enums import PhoneComponentStatus
    from app.phone.health import HealthComponent, agent_component_is_stale, channel_status

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

    # Read device snapshot
    device_snapshot = await session.scalar(
        select(PhoneDeviceSnapshot).where(PhoneDeviceSnapshot.id == "current")
    )

    # Read auto-answer state and active call from Redis
    from redis.exceptions import RedisError

    from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY, CALL_OWNED_KEY

    redis = None
    stopped = False
    owned = None
    try:
        redis = _admin_routes._phone_redis()
        stopped = (await redis.get(AUTO_ANSWER_STOPPED_KEY)) == "1"
        owned = await redis.get(CALL_OWNED_KEY)
    except (OSError, RedisError) as exc:
        logger.warning("phone_health_redis_unreachable", error=type(exc).__name__)
    finally:
        if redis is not None:
            await redis.aclose()

    active_call = None
    if owned:
        from app.models.entities import CommunicationSession

        try:
            call = await session.get(CommunicationSession, UUID(owned))
        except ValueError:
            call = None
        if call is not None and call.ended_at is None:
            active_call = {"session_id": owned, "script_stage": call.script_stage}

    return {
        "channel": channel_status(components).value if components else "unknown",
        "components": [
            {
                "component": c.component,
                "status": c.status.value,
                "detail": c.detail,
                "last_ok_at": c.last_ok_at,
            }
            for c in sorted(components, key=lambda c: c.component)
        ],
        "configured": get_settings().phone_agent_enabled,
        # ``updated_at`` is the snapshot row's own column, not part of the stored
        # payload; the template renders it with ``format_dt`` so it stays a datetime.
        "device": (
            {**device_snapshot.payload, "updated_at": device_snapshot.updated_at}
            if device_snapshot
            else {}
        ),
        "auto_answer": {
            "enabled": get_settings().phone_auto_answer_enabled,
            "stopped": stopped,
        },
        "active_call": active_call,
    }


_CALLS_PER_PAGE = 25
_CALLS_TABS = {"live", "history", "evidence"}
_REVIEW_FIELDS = {
    "interview_date",
    "interview_time",
    "timezone",
    "format",
    "address",
    "meeting_url",
    "company",
    "vacancy",
}


def _verification_label(status: str | None) -> str:
    return {
        "confirmed": "Подтверждено",
        "high_confidence": "Высокая уверенность",
        "needs_review": "Нужна проверка",
        "pending": "В очереди",
        "not_applicable": "Не применимо",
    }.get(status or "", "Нужна проверка")


def _verification_tone(status: str | None) -> str:
    return {
        "confirmed": "success",
        "high_confidence": "success",
        "needs_review": "warning",
        "pending": "muted",
    }.get(status or "", "muted")


def _apply_call_filter(stmt: Select[Any], filter_: str) -> Select[Any]:
    """Narrow a ``communication_sessions`` query by the history filter.

    RULING R2: this lives here for now; Task 15 lifts it into a shared helper.
    ``interview_proposed`` is intentionally not handled in SQL — see
    ``build_calls_context`` for why it is filtered in Python instead.
    """
    from app.models.entities import CommunicationSession
    from app.models.enums import CommunicationOutcome

    if filter_ == "needs_review":
        return stmt.where(
            or_(
                CommunicationSession.needs_review.is_(True),
                CommunicationSession.verification_status == "needs_review",
            )
        )
    if filter_ in {"confirmed", "high_confidence"}:
        return stmt.where(CommunicationSession.verification_status == filter_)
    if filter_ == "summary_failed":
        return stmt.where(CommunicationSession.summary_state == "failed")
    if filter_ == "telegram_failed":
        return stmt
    if filter_ == "missed_dropped":
        return stmt.where(
            CommunicationSession.outcome.in_(
                [CommunicationOutcome.MISSED, CommunicationOutcome.ABANDONED]
            )
        )
    if filter_ == "unknown_caller":
        return stmt.where(CommunicationSession.application_id.is_(None))
    return stmt


async def _call_row(session: AsyncSession, row: Any) -> dict[str, Any]:
    from app.models.entities import CanonicalJob
    from app.phone.numbers import mask_phone

    company: str | None = None
    vacancy: str | None = None
    if row.canonical_job_id is not None:
        job = await session.get(CanonicalJob, row.canonical_job_id)
        if job is not None:
            company = job.normalized_company
            vacancy = job.normalized_title
    duration_s: int | None = None
    if row.ended_at is not None and row.started_at is not None:
        duration_s = int((row.ended_at - row.started_at).total_seconds())
    summary: dict[str, Any] = row.summary or {}
    return {
        "id": str(row.id),
        "started_at": row.started_at,
        "company": company,
        "vacancy": vacancy,
        "caller": mask_phone(row.remote_address),
        "direction": row.direction.value,
        "duration_s": duration_s,
        "outcome": row.outcome.value if row.outcome is not None else None,
        "auto_answered": row.auto_answered,
        "needs_review": row.needs_review,
        "summary_state": row.summary_state.value,
        "telegram_state": (summary.get("telegram") or {}).get("state"),
        "verification_status": row.verification_status.value,
        "verification_label": _verification_label(row.verification_status.value),
        "verification_tone": _verification_tone(row.verification_status.value),
        "model_latency_ms": (
            (summary.get("verification") or {})
            .get("pass_metadata", {})
            .get("arbiter", {})
            .get("latency_ms")
            if isinstance((summary.get("verification") or {}).get("pass_metadata"), dict)
            else None
        ),
    }


async def _call_detail_context(session: AsyncSession, session_id: str) -> dict[str, Any] | None:
    """Build the session-detail block for ``?view=calls&session=<uuid>``.

    An invalid or unknown ``session_id`` returns ``None`` so the caller falls
    through to the history list (never a 404/500).
    """
    from app.models.entities import AuditEvent, CallFact, CommunicationSession, CommunicationTurn
    from app.models.enums import CommunicationChannel, TurnSpeaker
    from app.phone.numbers import mask_phone

    try:
        sid = UUID(session_id)
    except ValueError:
        return None

    call = await session.get(CommunicationSession, sid)
    if call is None:
        return None

    turns = list(
        (
            await session.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.session_id == call.id)
                .order_by(CommunicationTurn.seq)
            )
        ).all()
    )
    audits = list(
        (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.entity_id == str(call.id))
                .order_by(AuditEvent.timestamp)
            )
        ).all()
    )

    session_row = await _call_row(session, call)
    session_row.update(
        {
            "script_stage": call.script_stage,
            "diagnostics": call.diagnostics,
            "rx_frame_stats": call.rx_frame_stats,
        }
    )

    facts = list(
        (
            await session.scalars(
                select(CallFact).where(CallFact.session_id == call.id).order_by(CallFact.field)
            )
        ).all()
    )
    turns_by_id = {turn.id: turn for turn in turns}
    verification = (call.summary or {}).get("verification", {})
    if not isinstance(verification, dict):
        verification = {}
    field_labels = {
        "interview_date": "Дата",
        "interview_time": "Время",
        "timezone": "Часовой пояс",
        "format": "Формат",
        "address": "Адрес",
        "meeting_url": "Ссылка",
        "company": "Компания",
        "vacancy": "Вакансия",
    }
    fact_rows = []
    for fact in facts:
        source_turn = turns_by_id.get(fact.source_turn_id) if fact.source_turn_id else None
        fact_rows.append(
            {
                "id": str(fact.id),
                "field": fact.field,
                "label": field_labels.get(fact.field, fact.field),
                "value": fact.normalized_value,
                "raw_expression": fact.raw_expression,
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
                "confirmed_at": fact.confirmed_at,
                "source_quote": source_turn.text if source_turn else None,
                "evidence_url": (
                    f"/admin/phone/evidence/{call.id}/{source_turn.phonegate_transcript_id}.wav"
                    if source_turn
                    and source_turn.audio_evidence_path
                    and source_turn.phonegate_transcript_id is not None
                    else None
                ),
            }
        )
    sms_sessions = list(
        (
            await session.scalars(
                select(CommunicationSession)
                .where(
                    CommunicationSession.channel == CommunicationChannel.SMS,
                    CommunicationSession.profile_id == call.profile_id,
                )
                .order_by(CommunicationSession.started_at.desc())
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
    related_sms: list[dict[str, Any]] = []
    unlinked_sms: list[dict[str, Any]] = []
    for sms in sms_sessions:
        sms_row: dict[str, Any] = {
            "id": str(sms.id),
            "external_id": sms.transport_external_id,
            "remote_address": mask_phone(sms.remote_address),
            "started_at": sms.started_at,
            "needs_review": sms.needs_review,
            "linked": sms.related_session_id == call.id,
        }
        sms_turns = list(
            (
                await session.scalars(
                    select(CommunicationTurn)
                    .where(CommunicationTurn.session_id == sms.id)
                    .order_by(CommunicationTurn.seq)
                )
            ).all()
        )
        if sms.related_session_id == call.id:
            sms_row["text"] = sms_turns[0].text if sms_turns else None
            related_sms.append(sms_row)
        elif sms.related_session_id is None:
            # Metadata is useful for choosing a safe manual association. The
            # message body is only revealed after it is linked and detail is
            # authenticated.
            unlinked_sms.append(sms_row)

    pass_metadata = verification.get("pass_metadata", {})
    pass_results = verification.get("pass_results", {})
    model_decisions: list[dict[str, Any]] = []
    for name in ("extractor", "verifier", "arbiter"):
        result = pass_results.get(name, {}) if isinstance(pass_results, dict) else {}
        metadata = pass_metadata.get(name, {}) if isinstance(pass_metadata, dict) else {}
        model_decisions.append(
            {
                "name": name,
                "status": result.get("status") if isinstance(result, dict) else None,
                "reasons": (
                    result.get("review_reasons", result.get("reasons", []))
                    if isinstance(result, dict)
                    else []
                ),
                "latency_ms": metadata.get("latency_ms") if isinstance(metadata, dict) else None,
            }
        )
    reasons = verification.get("review_reason_codes", [])
    if not isinstance(reasons, list):
        reasons = []
    decision = verification.get("decision", {})
    if isinstance(decision, dict) and isinstance(decision.get("reasons"), list):
        reasons = list(dict.fromkeys([*reasons, *decision["reasons"]]))
    sms_comparisons = verification.get("sms_comparisons", [])
    if not isinstance(sms_comparisons, list):
        sms_comparisons = []
    sms_comparisons = [
        {
            "sms_turn_id": item.get("sms_turn_id"),
            "status": item.get("status"),
            "reason_codes": item.get("reason_codes", []),
            "comparisons": item.get("comparisons", []),
        }
        for item in sms_comparisons
        if isinstance(item, dict)
    ]

    return {
        "session": session_row,
        "summary": call.summary or {},
        "summary_state": call.summary_state.value,
        "verification_status": call.verification_status.value,
        "verification_label": _verification_label(call.verification_status.value),
        "verification_tone": _verification_tone(call.verification_status.value),
        "facts": fact_rows,
        "model_decisions": model_decisions,
        "review_reasons": reasons,
        "sms_comparisons": sms_comparisons,
        "related_sms": related_sms,
        "unlinked_sms": unlinked_sms,
        "turns": [
            {
                "seq": t.seq,
                "speaker": t.speaker.value,
                "text": (t.spoken_text if t.speaker is TurnSpeaker.ASSISTANT else t.text),
                "delivery_status": t.delivery_status.value,
                "asr_confidence": t.asr_confidence,
                "audio_evidence_url": (
                    f"/admin/phone/evidence/{call.id}/{t.phonegate_transcript_id}.wav"
                    if t.audio_evidence_path
                    else None
                ),
            }
            for t in turns
        ],
        "audit_events": [
            {
                "action": a.action,
                "at": a.timestamp.isoformat(),
                "decision": a.decision,
                "details": a.sanitized_details,
            }
            for a in audits
        ],
    }


async def _evidence_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """List every retained audio-evidence clip, newest sessions' turns first.

    Rows whose backing ``.wav`` file no longer exists on disk (retention sweep,
    manual cleanup) are silently skipped.
    """
    from app.models.entities import CanonicalJob, CommunicationSession, CommunicationTurn

    settings = get_settings()
    root = Path(settings.phone_evidence_dir)
    retention = timedelta(days=settings.phone_evidence_retention_days)

    turns = list(
        (
            await session.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.audio_evidence_path.is_not(None))
                .order_by(CommunicationTurn.session_id, CommunicationTurn.seq)
            )
        ).all()
    )

    rows: list[dict[str, Any]] = []
    for turn in turns:
        target = root / str(turn.session_id) / f"{turn.phonegate_transcript_id}.wav"
        try:
            mtime = target.stat().st_mtime
        except OSError:
            continue
        created_at = datetime.fromtimestamp(mtime, tz=UTC)

        call = await session.get(CommunicationSession, turn.session_id)
        company: str | None = None
        if call is not None and call.canonical_job_id is not None:
            job = await session.get(CanonicalJob, call.canonical_job_id)
            if job is not None:
                company = job.normalized_company

        rows.append(
            {
                "session_id": str(turn.session_id),
                "company": company,
                "started_at": call.started_at if call is not None else None,
                "seq": turn.seq,
                "text": turn.text,
                "asr_confidence": turn.asr_confidence,
                "created_at": created_at,
                "expires_at": created_at + retention,
                "url": (
                    f"/admin/phone/evidence/{turn.session_id}/{turn.phonegate_transcript_id}.wav"
                ),
            }
        )
    return rows


async def _manual_status(session: AsyncSession, call: Any) -> Any:
    """Derive a conservative trust status after an operator fact mutation."""
    from app.models.entities import CallFact
    from app.models.enums import CallFactState, PhoneVerificationStatus

    facts = list(
        (await session.scalars(select(CallFact).where(CallFact.session_id == call.id))).all()
    )
    if any(fact.state in {CallFactState.CONFLICT, CallFactState.UNKNOWN} for fact in facts):
        return PhoneVerificationStatus.NEEDS_REVIEW
    if facts and all(fact.state is CallFactState.CONFIRMED for fact in facts):
        return PhoneVerificationStatus.CONFIRMED
    if facts and all(fact.state is CallFactState.CANDIDATE for fact in facts):
        return PhoneVerificationStatus.HIGH_CONFIDENCE
    return PhoneVerificationStatus.PENDING


async def _get_call(session: AsyncSession, call_id: UUID) -> Any:
    from app.models.entities import CommunicationSession
    from app.models.enums import CommunicationChannel

    call = await session.get(CommunicationSession, call_id)
    if call is None or call.channel is not CommunicationChannel.CALL:
        raise HTTPException(status_code=404, detail="звонок не найден")
    return call


async def _get_sms_for_call(session: AsyncSession, call: Any, sms_id: UUID) -> Any:
    from app.models.entities import CommunicationSession
    from app.models.enums import CommunicationChannel, CommunicationDirection
    from app.phone.numbers import normalize_e164

    sms = await session.get(CommunicationSession, sms_id)
    if (
        sms is None
        or sms.channel is not CommunicationChannel.SMS
        or sms.direction is not CommunicationDirection.INBOUND
        or sms.profile_id != call.profile_id
        or (normalize_e164(sms.remote_address) or sms.remote_address)
        != (normalize_e164(call.remote_address) or call.remote_address)
    ):
        raise HTTPException(status_code=404, detail="SMS не найдено для этого звонка")
    return sms


async def _sms_turn(session: AsyncSession, sms: Any) -> Any:
    from app.models.entities import CommunicationTurn
    from app.models.enums import TurnSpeaker

    turns = list(
        (
            await session.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.session_id == sms.id)
                .order_by(CommunicationTurn.seq)
            )
        ).all()
    )
    turn = next((item for item in turns if item.speaker is TurnSpeaker.EMPLOYER), None)
    if turn is None:
        raise HTTPException(status_code=404, detail="SMS не содержит сообщения работодателя")
    return turn


@router.post("/admin/phone/calls/{call_id}/facts/{field}/review")
async def review_call_fact(
    call_id: UUID,
    field: str,
    request: Request,
    action: str = Form(...),
    value: str | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    if action not in {"confirm", "correct", "unknown"} or field not in _REVIEW_FIELDS:
        raise HTTPException(status_code=404, detail="операция недоступна")
    call = await _get_call(session, call_id)
    from app.models.entities import CallFact
    from app.models.enums import (
        CallFactConfirmationSource,
        CallFactState,
        PhoneVerificationStatus,
    )
    from app.phone.critical import CriticalField, normalize_critical_value
    from app.phone.notification_state import refresh_telegram_notification

    fact = await session.scalar(
        select(CallFact).where(CallFact.session_id == call.id, CallFact.field == field)
    )
    if fact is None:
        if action == "unknown":
            fact = CallFact(session_id=call.id, field=field, raw_expression="не указано")
            session.add(fact)
            await session.flush()
        else:
            raise HTTPException(status_code=404, detail="факт не найден")
    old_value = fact.normalized_value
    reference_at = call.started_at
    supplied = (value or fact.normalized_value or "").strip()[:500]
    normalized: str | None = None
    if action in {"confirm", "correct"}:
        if not supplied:
            raise HTTPException(status_code=422, detail="значение обязательно")
        normalized = normalize_critical_value(
            cast(CriticalField, field),
            supplied,
            reference_at=reference_at,
            timezone="Europe/Chisinau",
        )
        if normalized is None:
            raise HTTPException(status_code=422, detail="значение не прошло проверку")
        fact.raw_expression = supplied
        fact.normalized_value = normalized
        fact.state = CallFactState.CONFIRMED
        fact.confirmation_source = CallFactConfirmationSource.MANUAL
        fact.confirmed_at = datetime.now(UTC)
        fact.confirmed_by_turn_id = None
    else:
        fact.state = CallFactState.UNKNOWN
        fact.normalized_value = None
        fact.confirmed_by_turn_id = None
        fact.confirmation_source = None
        fact.confirmed_at = None

    status = await _manual_status(session, call)
    call.verification_status = status
    call.needs_review = status is PhoneVerificationStatus.NEEDS_REVIEW
    call.verification_revision += 1
    summary = dict(call.summary or {})
    verification = summary.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    verification["manual_review"] = {
        "field": field,
        "action": action,
        "new_normalized_value": normalized,
        "at": datetime.now(UTC).isoformat(),
    }
    summary["verification"] = verification
    call.summary = summary
    flag_modified(call, "summary")
    refresh_telegram_notification(call)
    flag_modified(call, "summary")
    action_name = {
        "confirm": "phone.fact.confirmed",
        "correct": "phone.fact.corrected",
        "unknown": "phone.fact.unknown",
    }[action]
    await _admin_routes._audit_admin(
        session,
        action_name,
        "communication_session",
        str(call.id),
        decision=action,
        details={
            "call_id": str(call.id),
            "fact_id": str(fact.id),
            "field": field,
            "old_normalized_value": old_value,
            "new_normalized_value": normalized,
        },
    )
    await session.commit()
    return RedirectResponse(f"/?view=calls&tab=history&session={call.id}", status_code=303)


@router.post("/admin/phone/calls/{call_id}/sms/{sms_id}/link")
async def link_call_sms(
    call_id: UUID,
    sms_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    call = await _get_call(session, call_id)
    sms = await _get_sms_for_call(session, call, sms_id)
    if sms.related_session_id is not None and sms.related_session_id != call.id:
        raise HTTPException(status_code=404, detail="SMS уже связано с другим звонком")
    turn = await _sms_turn(session, sms)
    summary = dict(call.summary or {})
    verification = summary.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    pending = verification.get("sms_pending", [])
    if not isinstance(pending, list):
        pending = []
    if str(turn.id) not in pending:
        pending.append(str(turn.id))
    input_ids = verification.get("sms_input_ids", [])
    if not isinstance(input_ids, list):
        input_ids = []
    if str(turn.id) not in input_ids:
        input_ids.append(str(turn.id))
    verification["sms_pending"] = pending
    verification["sms_input_ids"] = input_ids
    verification["sms_reconciliation"] = {
        "state": "linked_manually",
        "sms_turn_id": str(turn.id),
    }
    summary["verification"] = verification
    sms.related_session_id = call.id
    sms.needs_review = False
    call.summary = summary
    call.verification_revision += 1
    flag_modified(call, "summary")
    from app.phone.notification_state import refresh_telegram_notification

    refresh_telegram_notification(call)
    flag_modified(call, "summary")
    await _admin_routes._audit_admin(
        session,
        "phone.sms.linked",
        "communication_session",
        str(call.id),
        decision="linked",
        details={"call_id": str(call.id), "sms_id": str(sms.id), "manual": True},
    )
    await session.commit()
    return RedirectResponse(f"/?view=calls&tab=history&session={call.id}", status_code=303)


@router.post("/admin/phone/calls/{call_id}/sms/{sms_id}/unlink")
async def unlink_call_sms(
    call_id: UUID,
    sms_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    call = await _get_call(session, call_id)
    sms = await _get_sms_for_call(session, call, sms_id)
    if sms.related_session_id != call.id:
        raise HTTPException(status_code=404, detail="SMS не связано с этим звонком")
    from app.phone.facts import SmsConfirmationRejected, unlink_sms_confirmation

    try:
        await unlink_sms_confirmation(session, call=call, sms_session=sms)
    except SmsConfirmationRejected as exc:
        raise HTTPException(status_code=409, detail="SMS нельзя отвязать сейчас") from exc
    await _admin_routes._audit_admin(
        session,
        "phone.sms.unlinked",
        "communication_session",
        str(call.id),
        decision="unlinked",
        details={"call_id": str(call.id), "sms_id": str(sms.id)},
    )
    await session.commit()
    return RedirectResponse(f"/?view=calls&tab=history&session={call.id}", status_code=303)


async def build_calls_context(
    session: AsyncSession,
    *,
    tab: str,
    page: int,
    filter_: str,
    query: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the ``?view=calls`` template context (Live + История tabs)."""
    from app.models.entities import CanonicalJob, CommunicationSession
    from app.models.enums import CommunicationChannel

    valid_tab = tab if tab in _CALLS_TABS else "live"
    health = await phone_health_context(session)
    ctx: dict[str, Any] = {
        "tab": valid_tab,
        "filter": filter_,
        "query": query,
        "detail": None,
        "calls_health": health,
        "active_call": health.get("active_call"),
        "call_rows": [],
        "pagination": _admin_routes._pagination(0, 1, _CALLS_PER_PAGE),
    }
    if session_id is not None:
        ctx["detail"] = await _call_detail_context(session, session_id)

    if valid_tab == "evidence":
        ctx["evidence_rows"] = await _evidence_rows(session)
        return ctx

    if valid_tab != "history":
        return ctx

    stmt = select(CommunicationSession).where(
        CommunicationSession.channel == CommunicationChannel.CALL
    )
    if query:
        like = f"%{query}%"
        stmt = stmt.outerjoin(
            CanonicalJob, CommunicationSession.canonical_job_id == CanonicalJob.id
        ).where(
            or_(
                CanonicalJob.normalized_company.ilike(like),
                CanonicalJob.normalized_title.ilike(like),
                CommunicationSession.remote_address.ilike(like),
            )
        )
    stmt = _apply_call_filter(stmt, filter_).order_by(CommunicationSession.started_at.desc())

    if filter_ in {"interview_proposed", "telegram_failed"}:
        # JSON-path access (summary -> hints -> outcome_guess) is not portable
        # between SQLite (unit tests) and Postgres (prod), so fetch the filtered
        # set and narrow it in Python before paginating the resulting list.
        rows = list((await session.scalars(stmt)).all())
        if filter_ == "interview_proposed":
            matched = [
                row
                for row in rows
                if ((row.summary or {}).get("hints") or {}).get("outcome_guess")
                == "interview_proposed"
            ]
        else:
            matched = [
                row
                for row in rows
                if ((row.summary or {}).get("telegram") or {}).get("state") == "failed"
            ]
        pagination = _admin_routes._pagination(len(matched), page, _CALLS_PER_PAGE)
        start = (int(pagination["page"]) - 1) * _CALLS_PER_PAGE
        page_rows = matched[start : start + _CALLS_PER_PAGE]
    else:
        total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        pagination = _admin_routes._pagination(total, page, _CALLS_PER_PAGE)
        offset = (int(pagination["page"]) - 1) * _CALLS_PER_PAGE
        page_rows = list((await session.scalars(stmt.limit(_CALLS_PER_PAGE).offset(offset))).all())

    ctx["pagination"] = pagination
    ctx["call_rows"] = [await _call_row(session, row) for row in page_rows]
    return ctx


@router.post("/admin/phone/auto-answer/{action}")
async def phone_auto_answer_toggle(
    action: str,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    if action not in {"stop", "resume"}:
        raise HTTPException(status_code=404)
    from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY

    redis = _admin_routes._phone_redis()
    try:
        if action == "stop":
            await redis.set(AUTO_ANSWER_STOPPED_KEY, "1")
        else:
            await redis.delete(AUTO_ANSWER_STOPPED_KEY)
    finally:
        await redis.aclose()
    await _admin_routes._audit_admin(
        session, f"phone.auto_answer.{action}", "phone_channel", "auto_answer"
    )
    await session.commit()
    return RedirectResponse("/?view=diagnostics", status_code=303)


@router.get("/admin/phone/evidence/{session_id}/{transcript_id}.wav")
async def stream_evidence_clip(
    session_id: str,
    transcript_id: str,
    request: Request,
) -> Response:
    """Stream one retained audio-evidence clip to an authenticated admin.

    Both path params are validated structurally (UUID / digits) and the
    resolved file path is confirmed to live inside the evidence root before a
    single byte is read — a missing or escaping path is a 404, never a 500.
    """
    require_admin(request)
    try:
        sid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="запись недоступна") from exc
    if not transcript_id.isdigit():
        raise HTTPException(status_code=404, detail="запись недоступна")

    root = Path(get_settings().phone_evidence_dir).resolve()  # noqa: ASYNC240
    target = (root / str(sid) / f"{transcript_id}.wav").resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="запись недоступна")

    try:
        payload = target.read_bytes()
    except OSError as exc:
        # TOCTOU: prune_phone_evidence() (same ``phone`` queue) may unlink the
        # file between the is_file() check and this read — 404, never a 500.
        raise HTTPException(status_code=404, detail="запись недоступна") from exc

    return Response(
        payload,
        media_type="audio/wav",
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.post("/admin/phone/call/{session_id}/{action}")
async def phone_call_action(
    session_id: UUID,
    action: str,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    if action not in {"hangup", "mute"}:
        raise HTTPException(status_code=404)
    from app.phone.orchestrator import CALL_CMD_KEY, CALL_OWNED_KEY

    redis = _admin_routes._phone_redis()
    try:
        owned = await redis.get(CALL_OWNED_KEY)
        if owned != str(session_id):
            raise HTTPException(status_code=409, detail="not the active call")
        await redis.set(CALL_CMD_KEY, f"{action}:{session_id}", ex=60)
    finally:
        await redis.aclose()
    await _admin_routes._audit_admin(
        session, f"phone.call.{action}", "communication_session", str(session_id)
    )
    await session.commit()
    return RedirectResponse("/?view=diagnostics", status_code=303)
