from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from statistics import median
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
    TurnSpeaker,
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
    assistant_turns = [
        turn for turn in turns if turn.speaker == TurnSpeaker.ASSISTANT and turn.text.strip()
    ]
    employer_turns = [
        turn for turn in turns if turn.speaker == TurnSpeaker.EMPLOYER and turn.text.strip()
    ]
    calls_with_assistant = {turn.session_id for turn in assistant_turns}
    calls_with_employer = {turn.session_id for turn in employer_turns}
    assistant_only_calls = calls_with_assistant - calls_with_employer
    audio_turns = [turn for turn in turns if turn.audio_evidence_path]
    calls_with_audio = {turn.session_id for turn in audio_turns}
    calls_with_raw_rx_audio: set[UUID] = set()
    remote_hangup_call_ids: set[UUID] = set()
    remote_hangup_deltas: list[int] = []
    remote_hangups_without_delta = 0
    remote_hangups_during_tts = 0
    probable_prompt_rejections = 0
    for call in calls:
        diagnostics = call.diagnostics if isinstance(call.diagnostics, dict) else {}
        try:
            rx_bytes = int(diagnostics.get("rx_audio_bytes") or 0)
        except (TypeError, ValueError):
            rx_bytes = 0
        if rx_bytes > 0:
            calls_with_raw_rx_audio.add(call.id)
        if diagnostics.get("phonegate_end_reason") != "remote_or_network_hangup":
            continue
        remote_hangup_call_ids.add(call.id)
        raw_delta = diagnostics.get("peer_hangup_ms_after_last_tts")
        if isinstance(raw_delta, (int, float)) and raw_delta >= 0:
            remote_hangup_deltas.append(int(raw_delta))
        else:
            remote_hangups_without_delta += 1
        phase = str(diagnostics.get("remote_end_phase") or "")
        tts_phase = phase in {"intro_tts", "retry_tts", "details_tts"}
        if tts_phase:
            remote_hangups_during_tts += 1
        prompt_phase = tts_phase or phase in {
            "wait_first_rx", "wait_first_rx_retry",
        }
        if (
            call.id not in calls_with_employer
            and diagnostics.get("call_disposition") != "no_employer_response_while_connected"
            and (
                diagnostics.get("call_disposition") == "probable_prompt_rejection"
                or prompt_phase
            )
        ):
            probable_prompt_rejections += 1
    calls_with_any_rx_audio = calls_with_audio | calls_with_raw_rx_audio

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
                "assistant_transcript_turns": sum(
                    turn.speaker == TurnSpeaker.ASSISTANT and bool(turn.text.strip())
                    for turn in call_turns
                ),
                "employer_transcript_turns": sum(
                    turn.speaker == TurnSpeaker.EMPLOYER and bool(turn.text.strip())
                    for turn in call_turns
                ),
                "audio_evidence_available": (
                    any(turn.audio_evidence_path for turn in call_turns)
                    or call.id in calls_with_raw_rx_audio
                ),
                "lifecycle": {
                    "end_reason": (call.diagnostics or {}).get("phonegate_end_reason"),
                    "peer_hangup_ms_after_last_tts": (call.diagnostics or {}).get(
                        "peer_hangup_ms_after_last_tts"
                    ),
                    "rx_audio_bytes": (call.diagnostics or {}).get("rx_audio_bytes", 0),
                    "rx_audio_duration_ms": (call.diagnostics or {}).get(
                        "rx_audio_duration_ms", 0
                    ),
                    "audio_evidence_path": (call.diagnostics or {}).get(
                        "phonegate_audio_evidence_path"
                    ),
                    "disposition": (call.diagnostics or {}).get("call_disposition"),
                    "remote_end_phase": (call.diagnostics or {}).get("remote_end_phase"),
                },
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
            "assistant_transcript_turns": len(assistant_turns),
            "employer_transcript_turns": len(employer_turns),
            "calls_with_employer_transcript": len(calls_with_employer),
            "assistant_only_calls": len(assistant_only_calls),
            "audio_evidence_turns": len(audio_turns),
            "calls_with_audio_evidence": len(calls_with_audio),
            "calls_with_rx_audio": len(calls_with_any_rx_audio),
            "calls_with_rx_audio_but_no_employer_asr": len(
                calls_with_any_rx_audio - calls_with_employer
            ),
        },
        "hangups": {
            "remote_or_network_hangups": len(remote_hangup_call_ids),
            "remote_hangups_without_post_tts_delta": remote_hangups_without_delta,
            "remote_hangups_during_tts": remote_hangups_during_tts,
            "remote_hangups_within_1s_after_tts": sum(
                value <= 1000 for value in remote_hangup_deltas
            ),
            "remote_hangups_within_3s_after_tts": sum(
                value <= 3000 for value in remote_hangup_deltas
            ),
            "median_hangup_after_tts_ms": (
                round(median(remote_hangup_deltas)) if remote_hangup_deltas else None
            ),
            "probable_prompt_rejections": probable_prompt_rejections,
        },
        "analysis_items": analysis_items,
        "analysis_items_truncated": max(0, len(calls) - len(analysis_items)),
    }
