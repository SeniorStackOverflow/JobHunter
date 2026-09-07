"""Small dependency-free state transitions for phone notifications."""

from __future__ import annotations

from typing import Literal

from app.models.entities import CommunicationSession

TelegramNotificationState = Literal["pending", "disabled"]


def refresh_telegram_notification(
    call: CommunicationSession,
    *,
    state: TelegramNotificationState = "pending",
) -> None:
    """Bind notification work to the current verification revision.

    A sent record for the same revision is already the desired terminal state.
    Every other record is replaced with the small safe work envelope, so stale
    failures, claims and message IDs cannot survive a fact/trust update.
    """
    existing = call.summary.get("telegram") if isinstance(call.summary, dict) else None
    if isinstance(existing, dict):
        try:
            same_revision = int(existing.get("input_revision", -1)) == call.verification_revision
        except (TypeError, ValueError):
            same_revision = False
        if same_revision and existing.get("state") == "sent" and state == "pending":
            return
    call.summary = {
        **(call.summary if isinstance(call.summary, dict) else {}),
        "telegram": {
            "state": state,
            "input_revision": call.verification_revision,
            "attempts": 0,
            "next_attempt_at": None,
            "message_id": None,
            "ambiguous_delivery": False,
        },
    }


__all__ = ["TelegramNotificationState", "refresh_telegram_notification"]
