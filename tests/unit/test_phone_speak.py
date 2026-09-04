from __future__ import annotations

import pytest

from app.phone.client import PhoneGateClient, PhoneGateUnavailable
from app.phone.schemas import DeviceStatus
from app.phone.speak import (
    CallEnded,
    SpeakFenceTimeout,
    SpeakResult,
    observe_tx_delivery,
    speak_block,
    wait_until_speakable,
)
from tests.fixtures.fake_phonegate import FakePhoneGate

FAST = dict(timeout=2.0, poll=0.01)


@pytest.mark.asyncio
async def test_fence_ok_when_in_call_and_tx_idle() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        await wait_until_speakable(c, **FAST)  # returns, no raise


@pytest.mark.asyncio
async def test_fence_raises_when_call_ended() -> None:
    fake = FakePhoneGate()  # IDLE
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        with pytest.raises(CallEnded):
            await wait_until_speakable(c, **FAST)


@pytest.mark.asyncio
async def test_fence_times_out_while_tx_busy() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    fake.set_tx_auto_advance(False)  # TX stays busy forever
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        await c.speak("x")  # sets tx_preparing, never advances
        with pytest.raises(SpeakFenceTimeout):
            await wait_until_speakable(c, timeout=0.2, poll=0.01)


@pytest.mark.asyncio
async def test_speak_block_ok() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        res = await speak_block(c, "Здравствуйте", fence_timeout=2.0, poll=0.01)
        assert res.outcome == "ok"


@pytest.mark.asyncio
async def test_speak_block_ambiguous_timeout_is_unknown() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        fake.fail_next_speak(mode="timeout")
        res = await speak_block(c, "x", fence_timeout=2.0, poll=0.01)
        assert res.outcome == "unknown"


@pytest.mark.asyncio
async def test_speak_block_409_then_retry_succeeds() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        fake.fail_next_speak(mode="409_tx_busy")
        res = await speak_block(c, "x", fence_timeout=2.0, poll=0.01)
        assert res.outcome == "ok"


@pytest.mark.asyncio
async def test_speak_block_fence_transport_error_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PhoneGate blip while fencing must degrade, not propagate — an escaping
    exception would skip the caller's ``_hangup()`` and leave a live GSM call."""
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()

        async def boom() -> DeviceStatus:
            raise PhoneGateUnavailable("/api/device/status: ReadTimeout")

        monkeypatch.setattr(c, "device_status", boom)
        res = await speak_block(c, "x", fence_timeout=2.0, poll=0.01)
        assert res == SpeakResult(outcome="unknown")


@pytest.mark.asyncio
async def test_observe_tx_delivery_delivered() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        await c.speak("x")  # tx_preparing True; auto-advances on status polls
        result = await observe_tx_delivery(c, timeout=2.0, poll=0.01, start_grace=1.0)
        assert result == "delivered"


@pytest.mark.asyncio
async def test_observe_tx_delivery_failed_when_call_drops() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        await c.speak("x")
        await c.hangup()
        result = await observe_tx_delivery(c, timeout=2.0, poll=0.01, start_grace=0.05)
        assert result == "failed"
