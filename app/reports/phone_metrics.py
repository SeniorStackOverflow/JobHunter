from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    CommunicationSession,
    CommunicationTurn,
    InterviewAppointment,
    SourceJob,
)
from app.models.enums import (
    CommunicationChannel,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnDeliveryStatus,
)

_MAX_ANALYSIS_CALLS = 20
_MAX_TURNS_PER_CALL = 80


def _value(value: Any, default: str = "unknown") -> str:
    if value is None:
        return default
    raw = getattr(value, "value", value)
    return str(raw) if raw is not None else default


def _safe_error_code(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:128]


async def daily_phone_metrics(
    session: AsyncSession, start: datetime, end: datetime
) -> dict[str, Any]:
    calls = list(
        (
            await session.scalars(
                select(CommunicationSession)
                .where(
                    CommunicationSession.channel == CommunicationChannel.CALL,
                    CommunicationSession.started_at >= start,
                    CommunicationSession.started_at < end,
                )
                .order_by(CommunicationSession.started_at, CommunicationSession.id)
            )
        ).all()
    )
    call_ids = [call.id for call in calls]

    turns: list[CommunicationTurn] = []
    if call_ids:
        turns = list(
            (
                await session.scalars(
                    select(CommunicationTurn)
                    .where(CommunicationTurn.session_id.in_(call_ids))
                    .order_by(
                        CommunicationTurn.session_id,
                        CommunicationTurn.seq,
                        CommunicationTurn.id,
                    )
                )
            ).all()
        )

    turns_by_call: dict[UUID, list[CommunicationTurn]] = defaultdict(list)
    for turn in turns:
        turns_by_call[turn.session_id].append(turn)

    appointments_today = list(
        (
            await session.scalars(
                select(InterviewAppointment).where(
                    InterviewAppointment.created_at >= start,
                    InterviewAppointment.created_at < end,
                )
            )
        ).all()
    )
    linked_appointments: list[InterviewAppointment] = []
    if call_ids:
        linked_appointments = list(
            (
                await session.scalars(
                    select(InterviewAppointment).where(
                        InterviewAppointment.communication_session_id.in_(call_ids)
                    )
                )
            ).all()
        )

    jobs: dict[UUID, SourceJob] = {}
    job_ids = {call.source_job_id for call in calls if call.source_job_id is not None}
    if job_ids:
        job_rows = list(
            (await session.scalars(select(SourceJob).where(SourceJob.id.in_(job_ids)))).all()
        )
        jobs = {job.id: job for job in job_rows}

    outcome_counts = Counter(_value(call.outcome, "open") for call in calls)
    direction_counts = Counter(_value(call.direction) for call in calls)
    summary_counts = Counter(_value(call.summary_state) for call in calls)
    verification_counts = Counter(_value(call.verification_status) for call in calls)
    interview_status_counts = Counter(_value(item.status) for item in appointments_today)

    summary_failed_ids = {
        call.id for call in calls if call.summary_state == PhoneSummaryState.FAILED
    }
    failed_turns = [turn for turn in turns if turn.delivery_status == TurnDeliveryStatus.FAILED]
    failed_turn_call_ids = {turn.session_id for turn in failed_turns}
    technical_error_call_ids = summary_failed_ids | failed_turn_call_ids

    error_codes: Counter[str] = Counter()
    if failed_turns:
        error_codes["turn_delivery_failed"] = len(failed_turns)
    for call in calls:
        if call.summary_state != PhoneSummaryState.FAILED:
            continue
        summary = call.summary if isinstance(call.summary, dict) else {}
        model_meta: dict[str, Any] = {}
        raw_model_meta = summary.get("model_meta")
        if isinstance(raw_model_meta, dict):
            model_meta = raw_model_meta
        code = _safe_error_code(model_meta.get("last_error"))
        error_codes[f"summary:{code or 'failed'}"] += 1

    calls_with_transcript = {turn.session_id for turn in turns if turn.text.strip()}
    audio_turns = [turn for turn in turns if turn.audio_evidence_path]
    calls_with_audio = {turn.session_id for turn in audio_turns}

    linked_by_call: dict[UUID, list[InterviewAppointment]] = defaultdict(list)
    for appointment in linked_appointments:
        if appointment.communication_session_id is not None:
            linked_by_call[appointment.communication_session_id].append(appointment)

    analysis_items: list[dict[str, Any]] = []
    for call in calls[-_MAX_ANALYSIS_CALLS:]:
        call_turns = turns_by_call.get(call.id, [])
        visible_turns = call_turns[:_MAX_TURNS_PER_CALL]
        summary = call.summary if isinstance(call.summary, dict) else {}
        hints: dict[str, Any] = {}
        raw_hints = summary.get("hints")
        if isinstance(raw_hints, dict):
            hints = raw_hints
        job = jobs.get(call.source_job_id) if call.source_job_id is not None else None
        analysis_items.append(
            {
                "session_id": str(call.id),
                "started_at": call.started_at.isoformat(),
                "ended_at": call.ended_at.isoformat() if call.ended_at else None,
                "direction": _value(call.direction),
                "outcome": _value(call.outcome, "open"),
                "answered": call.answered_at is not None,
                "needs_review": bool(call.needs_review),
                "summary_state": _value(call.summary_state),
                "verification_status": _value(call.verification_status),
                "outcome_guess": hints.get("outcome_guess"),
                "job_title": job.title if job else None,
                "company": job.company if job else None,
                "transcript": [
                    {
                        "seq": turn.seq,
                        "speaker": _value(turn.speaker),
                        "text": turn.text,
                        "asr_confidence": turn.asr_confidence,
                        "delivery_status": _value(turn.delivery_status),
                        "audio_evidence_path": turn.audio_evidence_path,
                    }
                    for turn in visible_turns
                ],
                "transcript_truncated": len(call_turns) > len(visible_turns),
                "audio_evidence_available": any(
                    turn.audio_evidence_path for turn in call_turns
                ),
                "interviews": [
                    {
                        "status": _value(item.status),
                        "starts_at": item.starts_at.isoformat() if item.starts_at else None,
                        "timezone": item.timezone,
                        "format": _value(item.format),
                        "address": item.address,
                        "contact_person": item.contact_person,
                    }
                    for item in linked_by_call.get(call.id, [])
                ],
            }
        )

    return {
        "total": len(calls),
        "answered": sum(call.answered_at is not None for call in calls),
        "by_outcome": dict(sorted(outcome_counts.items())),
        "by_direction": dict(sorted(direction_counts.items())),
        "needs_review": sum(call.needs_review for call in calls),
        "verification_needs_review": sum(
            call.verification_status == PhoneVerificationStatus.NEEDS_REVIEW for call in calls
        ),
        "summary_states": dict(sorted(summary_counts.items())),
        "verification_statuses": dict(sorted(verification_counts.items())),
        "errors": {
            "calls_with_technical_errors": len(technical_error_call_ids),
            "summary_failed_calls": len(summary_failed_ids),
            "turn_delivery_failures": len(failed_turns),
            "by_code": dict(sorted(error_codes.items())),
        },
        "interviews": {
            "created_today": len(appointments_today),
            "linked_to_today_calls": len(linked_appointments),
            "confirmed": interview_status_counts.get("confirmed", 0),
            "proposed": interview_status_counts.get("proposed", 0),
            "needs_review": interview_status_counts.get("needs_review", 0),
            "cancelled": interview_status_counts.get("cancelled", 0),
            "by_status": dict(sorted(interview_status_counts.items())),
        },
        "evidence": {
            "transcript_turns": len(turns),
            "calls_with_transcript": len(calls_with_transcript),
            "audio_evidence_turns": len(audio_turns),
            "calls_with_audio_evidence": len(calls_with_audio),
        },
        "analysis_items": analysis_items,
        "analysis_items_truncated": max(0, len(calls) - len(analysis_items)),
    }
