from __future__ import annotations

import httpx
import pytest

from app.phone.client import (
    PhoneGateBusy,
    PhoneGateClient,
    PhoneGateError,
    PhoneGateUnavailable,
)
from app.phone.schemas import PhoneSmsPage
from tests.fixtures.fake_phonegate import FakePhoneGate


@pytest.mark.asyncio
async def test_client_reads_status_and_events() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(
        base_url="http://phonegate", token="test", transport=fake.transport()
    ) as client:
        status = await client.device_status()
        assert status.call_state == "RINGING"
        page = await client.events(after_id=0)
        assert page.events[0].type == "call_state"


@pytest.mark.asyncio
async def test_client_maps_errors() -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/device/status":
            return httpx.Response(503, json={"detail": "down"})
        return httpx.Response(404, json={"detail": "nope"})

    transport = httpx.MockTransport(_handler)
    async with PhoneGateClient(
        base_url="http://phonegate", token="t", transport=transport
    ) as client:
        with pytest.raises(PhoneGateUnavailable):
            await client.device_status()
        with pytest.raises(PhoneGateError):
            await client.events(after_id=0)


@pytest.mark.asyncio
async def test_client_maps_non_json_200_to_phonegate_error() -> None:
    """F2 / BLOCKER: a 200 with a non-JSON body must surface as PhoneGateError,
    not a bare JSONDecodeError that escapes run_cycle and kills the process."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>gateway error</html>")

    async with PhoneGateClient(
        base_url="http://phonegate", token="t", transport=httpx.MockTransport(_handler)
    ) as client:
        with pytest.raises(PhoneGateError):
            await client.device_status()


@pytest.mark.asyncio
async def test_client_maps_wrong_shape_200_to_phonegate_error() -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        # valid JSON, wrong type for a nested model -> ValidationError
        return httpx.Response(200, json={"rx_audio_stats": "not-an-object"})

    async with PhoneGateClient(
        base_url="http://phonegate", token="t", transport=httpx.MockTransport(_handler)
    ) as client:
        with pytest.raises(PhoneGateError):
            await client.device_status()


@pytest.mark.asyncio
async def test_events_skips_one_malformed_event() -> None:
    """F2 / BLOCKER: one bad element in the events list must not fail the page."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "events": [
                    {"id": 1, "type": "call_state", "data": {}},
                    {"id": "not-an-int", "type": "transcript"},
                ],
                "latest_id": 2,
            },
        )

    async with PhoneGateClient(
        base_url="http://phonegate", token="t", transport=httpx.MockTransport(_handler)
    ) as client:
        page = await client.events(after_id=0)
    assert [e.id for e in page.events] == [1]
    assert page.latest_id == 2


@pytest.mark.asyncio
async def test_events_carries_boot_id() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(
        base_url="http://phonegate", token="t", transport=fake.transport()
    ) as client:
        status = await client.device_status()
        page = await client.events(after_id=0)
    assert status.boot_id
    assert page.boot_id == status.boot_id


@pytest.mark.asyncio
async def test_events_rejects_page_without_latest_id() -> None:
    """F1/F2 review / HIGH: latest_id drives reset detection — a page that omits
    it must not be read as 'gateway at id 0' (would force a spurious reset)."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"events": [{"id": 7, "type": "call_state", "data": {}}]})

    async with PhoneGateClient(
        base_url="http://phonegate", token="t", transport=httpx.MockTransport(_handler)
    ) as client:
        with pytest.raises(PhoneGateError, match="latest_id"):
            await client.events(after_id=0)


@pytest.mark.asyncio
async def test_write_methods_against_fake() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        assert (await c.device_status()).call_state == "IN_CALL"
        await c.speak("Здравствуйте")
        await c.hangup()
        assert (await c.device_status()).call_state == "IDLE"


@pytest.mark.asyncio
async def test_speak_409_raises_phonegate_busy() -> None:
    fake = FakePhoneGate()  # IDLE -> speak 409
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        with pytest.raises(PhoneGateBusy):
            await c.speak("x")


@pytest.mark.asyncio
async def test_speak_409_with_non_object_body_still_raises_busy() -> None:
    """A 409 whose JSON body is not an object must still map to PhoneGateBusy
    (``.get()`` on a list/str raises AttributeError, not ValueError)."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=["tx busy"])

    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=httpx.MockTransport(_handler)
    ) as c:
        with pytest.raises(PhoneGateBusy):
            await c.speak("x")


@pytest.mark.asyncio
async def test_speak_504_raises_unavailable() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c:
        await c.answer()
        fake.fail_next_speak(mode="timeout")
        with pytest.raises(PhoneGateUnavailable):
            await c.speak("x")


@pytest.mark.asyncio
async def test_client_maps_transport_failure() -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with PhoneGateClient(
        base_url="http://phonegate", token="t", transport=httpx.MockTransport(_boom)
    ) as client:
        with pytest.raises(PhoneGateUnavailable):
            await client.health()


@pytest.mark.asyncio
async def test_recent_call_audio_returns_wav_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/call/audio"
        assert request.url.params["seconds"] == "8"
        return httpx.Response(200, content=b"RIFFfake", headers={"content-type": "audio/wav"})

    client = PhoneGateClient(
        base_url="http://pg", token="t", transport=httpx.MockTransport(handler)
    )
    assert await client.recent_call_audio(8) == b"RIFFfake"
    await client.aclose()


@pytest.mark.asyncio
async def test_recent_call_audio_409_raises_phonegate_error():
    client = PhoneGateClient(
        base_url="http://pg",
        token="t",
        transport=httpx.MockTransport(lambda r: httpx.Response(409, json={"success": False})),
    )
    with pytest.raises(PhoneGateError):
        await client.recent_call_audio(5)
    await client.aclose()


@pytest.mark.asyncio
async def test_recent_call_audio_clamps_seconds():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["seconds"] = request.url.params["seconds"]
        return httpx.Response(200, content=b"x")

    client = PhoneGateClient(
        base_url="http://pg", token="t", transport=httpx.MockTransport(handler)
    )
    await client.recent_call_audio(99)
    assert seen["seconds"] == "10"
    await client.aclose()


@pytest.mark.asyncio
async def test_sms_history_uses_bearer_query_and_lenient_message_enrichment() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        seen["query"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "id": "m-1",
                        "address": "+373 60 111 222",
                        "text": "Встреча завтра",
                        "timestamp": 1_720_000_000_000,
                        "direction": "incoming",
                        "status": "received",
                        "contact_name": "Employer enrichment",
                    }
                ],
                "count": 1,
                "synced_at": 1_720_000_000_000,
                "syncing": False,
            },
        )

    async with PhoneGateClient(
        base_url="http://phonegate", token="secret", transport=httpx.MockTransport(handler)
    ) as client:
        page = await client.sms_history(limit=999, number="+37360000000")

    assert isinstance(page, PhoneSmsPage)
    assert seen["authorization"] == "Bearer secret"
    assert seen["query"] == {"limit": "150", "number": "+37360000000"}
    assert page.messages[0].id == "m-1"


@pytest.mark.asyncio
async def test_sms_history_rejects_unknown_top_level_keys() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"messages": [], "count": 0, "unexpected": "reject me"},
        )

    async with PhoneGateClient(
        base_url="http://phonegate", token="secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(PhoneGateError):
            await client.sms_history()


@pytest.mark.asyncio
async def test_sync_sms_posts_without_outbound_sms_method() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(202, json={"success": True})

    async with PhoneGateClient(
        base_url="http://phonegate", token="secret", transport=httpx.MockTransport(handler)
    ) as client:
        await client.sync_sms()

    assert seen == {"method": "POST", "path": "/api/sms/sync"}
    assert not hasattr(client, "send_sms")


@pytest.mark.asyncio
async def test_sms_history_preserves_existing_error_classification() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    async with PhoneGateClient(
        base_url="http://phonegate", token="secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(PhoneGateUnavailable):
            await client.sms_history()
