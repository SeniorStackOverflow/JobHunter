from __future__ import annotations

from datetime import timedelta

from app.models.entities import (
    CommunicationSession,
    CommunicationTurn,
    InterviewAppointment,
    UserProfile,
)
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    InterviewStatus,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnDeliveryStatus,
    TurnSpeaker,
)
from app.reports.phone_metrics import daily_phone_metrics
from app.time_utils import local_day_bounds


async def test_daily_phone_metrics_counts_calls_errors_interviews_and_evidence(
    sqlite_session_factory,
) -> None:
    _local, start, end = local_day_bounds()
    now = start + timedelta(hours=12)

    async with sqlite_session_factory() as session:
        profile = UserProfile(name="Phone report candidate", is_default=True)
        session.add(profile)
        await session.flush()

        completed = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            started_at=now,
            answered_at=now + timedelta(seconds=3),
            ended_at=now + timedelta(minutes=1),
            outcome=CommunicationOutcome.COMPLETED,
            summary_state=PhoneSummaryState.DONE,
            verification_status=PhoneVerificationStatus.HIGH_CONFIDENCE,
            summary={"hints": {"outcome_guess": "interview_proposed"}},
        )
        failed_summary = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            started_at=now + timedelta(minutes=5),
            answered_at=now + timedelta(minutes=5, seconds=2),
            ended_at=now + timedelta(minutes=6),
            outcome=CommunicationOutcome.UNKNOWN,
            needs_review=True,
            summary_state=PhoneSummaryState.FAILED,
            verification_status=PhoneVerificationStatus.NEEDS_REVIEW,
            summary={"model_meta": {"last_error": "verification_timeout"}},
        )
        session.add_all([completed, failed_summary])
        await session.flush()

        session.add_all(
            [
                CommunicationTurn(
                    session_id=completed.id,
                    seq=1,
                    speaker=TurnSpeaker.EMPLOYER,
                    text="Приходите завтра на собеседование в 14:00.",
                    asr_confidence=0.92,
                    delivery_status=TurnDeliveryStatus.NOT_APPLICABLE,
                    audio_evidence_path="/evidence/call-1/1.wav",
                    occurred_at=now + timedelta(seconds=10),
                ),
                CommunicationTurn(
                    session_id=failed_summary.id,
                    seq=1,
                    speaker=TurnSpeaker.ASSISTANT,
                    text="Повторите, пожалуйста.",
                    delivery_status=TurnDeliveryStatus.FAILED,
                    occurred_at=now + timedelta(minutes=5, seconds=10),
                ),
                InterviewAppointment(
                    profile_id=profile.id,
                    communication_session_id=completed.id,
                    starts_at=now + timedelta(days=1, hours=2),
                    status=InterviewStatus.CONFIRMED,
                    confirmed_at=now,
                    created_at=now,
                ),
                InterviewAppointment(
                    profile_id=profile.id,
                    communication_session_id=failed_summary.id,
                    starts_at=None,
                    status=InterviewStatus.NEEDS_REVIEW,
                    created_at=now + timedelta(minutes=6),
                ),
            ]
        )
        await session.flush()

        metrics = await daily_phone_metrics(session, start, end)

    assert metrics["total"] == 2
    assert metrics["answered"] == 2
    assert metrics["by_outcome"] == {"completed": 1, "unknown": 1}
    assert metrics["needs_review"] == 1
    assert metrics["verification_needs_review"] == 1
    assert metrics["errors"] == {
        "calls_with_technical_errors": 1,
        "summary_failed_calls": 1,
        "turn_delivery_failures": 1,
        "by_code": {
            "summary:verification_timeout": 1,
            "turn_delivery_failed": 1,
        },
    }
    assert metrics["interviews"]["created_today"] == 2
    assert metrics["interviews"]["linked_to_today_calls"] == 2
    assert metrics["interviews"]["confirmed"] == 1
    assert metrics["interviews"]["needs_review"] == 1
    assert metrics["evidence"] == {
        "transcript_turns": 2,
        "calls_with_transcript": 2,
        "audio_evidence_turns": 1,
        "calls_with_audio_evidence": 1,
    }
    assert len(metrics["analysis_items"]) == 2
    first = metrics["analysis_items"][0]
    assert first["outcome_guess"] == "interview_proposed"
    assert first["audio_evidence_available"] is True
    assert first["transcript"][0]["speaker"] == "employer"
    assert first["interviews"][0]["status"] == "confirmed"
