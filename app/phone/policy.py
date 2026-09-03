from __future__ import annotations

from dataclasses import dataclass

from app.phone.schemas import DeviceStatus
from app.settings.config import Settings


@dataclass(frozen=True, slots=True)
class AnswerDecision:
    answer: bool
    reason: str


def should_answer(
    *,
    status: DeviceStatus,
    settings: Settings,
    runtime_stopped: bool,
    normalized_caller: str | None,
) -> AnswerDecision:
    if not settings.phone_auto_answer_enabled:
        return AnswerDecision(False, "disabled_by_config")
    if runtime_stopped:
        return AnswerDecision(False, "stopped_by_operator")
    if normalized_caller is not None and normalized_caller in settings.phone_answer_blocklist:
        return AnswerDecision(False, "blocklisted")
    if status.call_state != "RINGING":
        return AnswerDecision(False, "not_ringing")
    return AnswerDecision(True, "answer")
