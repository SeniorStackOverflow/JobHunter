from __future__ import annotations

from enum import StrEnum


class TelephonyState(StrEnum):
    IDLE = "idle"
    RINGING = "ringing"
    CONNECTED = "connected"
    ENDED = "ended"


def telephony_state_from_call_state(value: str) -> TelephonyState:
    return {
        "RINGING": TelephonyState.RINGING,
        "IN_CALL": TelephonyState.CONNECTED,
    }.get(value, TelephonyState.IDLE)
