from __future__ import annotations

from app.models.enums import (
    CallFactConfirmationSource,
    CallFactState,
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    ContactType,
    InterviewFormat,
    InterviewStatus,
    PhoneComponentStatus,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnSpeaker,
)


def test_contact_type_has_phone() -> None:
    assert ContactType.PHONE == "phone"


def test_communication_enums_values() -> None:
    assert CommunicationChannel.CALL == "call"
    assert CommunicationDirection.INBOUND == "inbound"
    assert set(CommunicationOutcome) == {"missed", "completed", "abandoned", "unknown"}
    assert set(TurnSpeaker) == {"employer", "assistant", "operator", "system"}


def test_fact_and_interview_enums_values() -> None:
    assert set(CallFactState) == {"candidate", "confirmed", "conflict", "unknown"}
    assert set(InterviewFormat) == {"onsite", "remote", "phone", "unknown"}
    assert set(InterviewStatus) == {"proposed", "confirmed", "needs_review", "cancelled"}
    assert set(PhoneComponentStatus) == {"healthy", "degraded", "unavailable", "unknown"}


def test_phone_verification_enums_values() -> None:
    assert set(PhoneSummaryState) == {
        "not_applicable",
        "pending",
        "processing",
        "done",
        "failed",
        "skipped",
    }
    assert set(PhoneVerificationStatus) == {
        "not_applicable",
        "pending",
        "confirmed",
        "high_confidence",
        "needs_review",
    }
    assert set(CallFactConfirmationSource) == {"sms", "manual"}
