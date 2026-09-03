from __future__ import annotations

import httpx
import pytest

from app.phone.client import PhoneGateClient, PhoneGateError, PhoneGateUnavailable
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
async def test_client_maps_transport_failure() -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with PhoneGateClient(
        base_url="http://phonegate", token="t", transport=httpx.MockTransport(_boom)
    ) as client:
        with pytest.raises(PhoneGateUnavailable):
            await client.health()
