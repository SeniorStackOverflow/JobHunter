from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from app.phone.client import (
    PhoneGateBusy,
    PhoneGateClient,
    PhoneGateError,
    PhoneGateUnavailable,
)

logger = structlog.get_logger(__name__)

_ACTIVE_STATES = {"IN_CALL"}


class CallEnded(Exception):
    """The call left IN_CALL while we were waiting to speak."""


class SpeakFenceTimeout(Exception):
    """TX did not become idle within the fence timeout."""


async def wait_until_speakable(
    client: PhoneGateClient,
    *,
    timeout: float,  # noqa: ASYNC109 - deadline-driven poll fence, not a cancel scope
    poll: float,
) -> None:
    """Block until PhoneGate is ready for a fresh utterance.

    Returns when the call is IN_CALL and no TX is active or preparing. Raises
    :class:`CallEnded` if the call left IN_CALL, or :class:`SpeakFenceTimeout` if
    TX never went idle before the deadline.
    """
    deadline = time.monotonic() + timeout
    while True:
        status = await client.device_status()
        if status.call_state not in _ACTIVE_STATES:
            raise CallEnded(status.call_state)
        if not status.tx_active and not status.tx_preparing:
            return
        if time.monotonic() >= deadline:
            raise SpeakFenceTimeout
        await asyncio.sleep(poll)


@dataclass(frozen=True, slots=True)
class SpeakResult:
    outcome: str  # "ok" | "ended" | "unknown"


async def speak_block(
    client: PhoneGateClient, text: str, *, fence_timeout: float, poll: float
) -> SpeakResult:
    """Fence on TX state, then POST the utterance with idempotent 409 handling.

    A 409 (``PhoneGateBusy``) buys exactly one bounded retry: re-fence, then one
    more ``speak``. An ambiguous transport timeout (``PhoneGateUnavailable``) on
    the first attempt is never retried — a duplicate assistant utterance is worse
    than a missed one — and yields ``SpeakResult("unknown")``.
    """
    try:
        await wait_until_speakable(client, timeout=fence_timeout, poll=poll)
    except CallEnded:
        return SpeakResult("ended")
    except SpeakFenceTimeout:
        logger.warning("phone_speak_fence_timeout")
        return SpeakResult("unknown")
    except (PhoneGateUnavailable, PhoneGateError):
        logger.warning("phone_speak_fence_transport_error")
        return SpeakResult("unknown")

    try:
        await client.speak(text)
        return SpeakResult("ok")
    except PhoneGateBusy as exc:
        # Re-fence and take exactly one bounded retry.
        try:
            await wait_until_speakable(client, timeout=fence_timeout, poll=poll)
        except CallEnded:
            return SpeakResult("ended")
        except SpeakFenceTimeout:
            return SpeakResult("unknown")
        except (PhoneGateUnavailable, PhoneGateError):
            logger.warning("phone_speak_fence_transport_error")
            return SpeakResult("unknown")
        try:
            await client.speak(text)
            return SpeakResult("ok")
        except PhoneGateBusy:
            logger.warning("phone_speak_still_busy_after_retry", detail=exc.detail)
            return SpeakResult("unknown")
        except PhoneGateUnavailable:
            return SpeakResult("unknown")
        except PhoneGateError:
            return SpeakResult("unknown")
    except PhoneGateUnavailable:
        # Ambiguous: PhoneGate may already have accepted the utterance. Never retry.
        logger.warning("phone_speak_ambiguous_timeout")
        return SpeakResult("unknown")
    except PhoneGateError:
        return SpeakResult("unknown")


async def observe_tx_delivery(
    client: PhoneGateClient,
    *,
    timeout: float,  # noqa: ASYNC109 - deadline-driven poll loop, not a cancel scope
    poll: float,
    start_grace: float,
) -> str:
    """After a 200 from ``/speak``, watch the TX state to decide the turn's fate.

    Returns one of ``"delivered"`` (TX activated then returned to idle),
    ``"failed"`` (call ended, or TX never activated within ``start_grace``), or
    ``"delivery_unknown"`` (TX stuck past ``timeout``, or PhoneGate went away).
    These strings are the exact ``TurnDeliveryStatus`` enum values.
    """
    start = time.monotonic()
    deadline = start + timeout
    saw_active = False
    while True:
        try:
            status = await client.device_status()
        except (PhoneGateUnavailable, PhoneGateError):
            return "delivery_unknown"
        if status.call_state not in _ACTIVE_STATES:
            return "delivered" if saw_active else "failed"
        if status.tx_active or status.tx_preparing:
            saw_active = True
        elif saw_active:
            return "delivered"
        elif time.monotonic() - start >= start_grace:
            return "failed"
        if time.monotonic() >= deadline:
            return "delivery_unknown"
        await asyncio.sleep(poll)
