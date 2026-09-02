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
async def test_client_maps_transport_failure() -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with PhoneGateClient(
        base_url="http://phonegate", token="t", transport=httpx.MockTransport(_boom)
    ) as client:
        with pytest.raises(PhoneGateUnavailable):
            await client.health()
