from datetime import UTC, datetime
from uuid import uuid4

from app.models.entities import CommunicationSession
from app.models.enums import CommunicationChannel, CommunicationDirection, PhoneVerificationStatus
from app.phone.notification_state import refresh_telegram_notification


def _call(*, revision: int, telegram: dict | None = None) -> CommunicationSession:
    return CommunicationSession(
        id=uuid4(),
        profile_id=uuid4(),
        channel=CommunicationChannel.CALL,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        started_at=datetime.now(UTC),
        verification_status=PhoneVerificationStatus.NEEDS_REVIEW,
        verification_revision=revision,
        summary={"telegram": telegram} if telegram is not None else {},
    )


def test_refreshes_old_sent_revision_to_pending_current_revision() -> None:
    call = _call(
        revision=2,
        telegram={"state": "sent", "input_revision": 1, "message_id": 8, "attempts": 1},
    )
    refresh_telegram_notification(call)
    assert call.summary["telegram"] == {
        "state": "pending",
        "input_revision": 2,
        "attempts": 0,
        "next_attempt_at": None,
        "message_id": None,
        "ambiguous_delivery": False,
    }


def test_duplicate_same_revision_preserves_sent_record() -> None:
    call = _call(
        revision=2,
        telegram={"state": "sent", "input_revision": 2, "message_id": 8, "attempts": 1},
    )
    refresh_telegram_notification(call)
    assert call.summary["telegram"]["state"] == "sent"
    assert call.summary["telegram"]["message_id"] == 8


def test_disabled_policy_is_explicit() -> None:
    call = _call(revision=3)
    refresh_telegram_notification(call, state="disabled")
    assert call.summary["telegram"]["state"] == "disabled"
    assert call.summary["telegram"]["input_revision"] == 3
