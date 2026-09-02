from __future__ import annotations

import httpx
import pytest

from tests.fixtures.fake_phonegate import FakePhoneGate


@pytest.mark.asyncio
async def test_scripted_call_produces_ordered_events() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    fake.answer()
    tid = fake.transcript(speaker="rx", text="Здравствуйте")
    fake.hangup()

    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="http://phonegate",
        headers={"Authorization": "Bearer test"},
    ) as client:
        status = (await client.get("/api/device/status")).json()
        assert status["call_state"] == "IDLE"
        assert status["latest_event_id"] >= 4

        events = (await client.get("/api/events", params={"after_id": 0})).json()
        types = [e["type"] for e in events["events"]]
        assert types[0] == "call_state"
        assert types[1] == "incoming_call"
        assert "transcript" in types
        assert events["events"] == sorted(events["events"], key=lambda e: e["id"])

    assert tid == 1


@pytest.mark.asyncio
async def test_requires_bearer() -> None:
    fake = FakePhoneGate()
    async with httpx.AsyncClient(transport=fake.transport(), base_url="http://phonegate") as client:
        assert (await client.get("/api/device/status")).status_code == 401


@pytest.mark.asyncio
async def test_restart_resets_event_ids() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    fake.restart()
    fake.transcript(speaker="rx", text="after restart")
    async with httpx.AsyncClient(
        transport=fake.transport(),
        base_url="http://phonegate",
        headers={"Authorization": "Bearer t"},
    ) as client:
        events = (await client.get("/api/events", params={"after_id": 0})).json()
        assert events["latest_id"] == 1
