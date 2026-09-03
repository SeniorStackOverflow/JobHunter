from __future__ import annotations

import httpx
import pytest

from tests.fixtures.fake_phonegate import FakePhoneGate
from tests.fixtures.fake_redis import FakeAsyncRedis


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
async def test_answer_speak_hangup_and_tx_cycle() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    transport = fake.transport()
    async with httpx.AsyncClient(transport=transport, base_url="http://pg") as c:
        # speak before answer -> 409
        r = await c.post(
            "/api/call/speak", json={"text": "hi"}, headers={"authorization": "Bearer t"}
        )
        assert r.status_code == 409

        r = await c.post("/api/call/answer", headers={"authorization": "Bearer t"})
        assert r.status_code == 200
        st = (await c.get("/api/device/status", headers={"authorization": "Bearer t"})).json()
        assert st["call_state"] == "IN_CALL"

        r = await c.post(
            "/api/call/speak", json={"text": "Здравствуйте"}, headers={"authorization": "Bearer t"}
        )
        assert r.status_code == 200
        # a tx transcript line was written
        tr = (
            await c.get("/api/call/transcript?after_id=0", headers={"authorization": "Bearer t"})
        ).json()
        assert any(e["speaker"] == "tx" and e["text"] == "Здравствуйте" for e in tr["entries"])
        # TX runs preparing -> active -> idle across status polls
        seen = []
        for _ in range(4):
            s = (await c.get("/api/device/status", headers={"authorization": "Bearer t"})).json()
            seen.append((s["tx_preparing"], s["tx_active"]))
        assert (True, False) in seen and (False, True) in seen and seen[-1] == (False, False)

        r = await c.post("/api/call/hangup", headers={"authorization": "Bearer t"})
        assert r.status_code == 200
        st = (await c.get("/api/device/status", headers={"authorization": "Bearer t"})).json()
        assert st["call_state"] == "IDLE"


@pytest.mark.asyncio
async def test_fail_next_speak_409_and_timeout() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with httpx.AsyncClient(transport=fake.transport(), base_url="http://pg") as c:
        await c.post("/api/call/answer", headers={"authorization": "Bearer t"})
        fake.fail_next_speak(mode="409_tx_busy")
        r = await c.post(
            "/api/call/speak", json={"text": "x"}, headers={"authorization": "Bearer t"}
        )
        assert r.status_code == 409
        # next call succeeds
        r = await c.post(
            "/api/call/speak", json={"text": "y"}, headers={"authorization": "Bearer t"}
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_fail_next_speak_timeout_returns_504() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    async with httpx.AsyncClient(transport=fake.transport(), base_url="http://pg") as c:
        await c.post("/api/call/answer", headers={"authorization": "Bearer t"})
        fake.fail_next_speak(mode="timeout")
        r = await c.post(
            "/api/call/speak", json={"text": "x"}, headers={"authorization": "Bearer t"}
        )
        assert r.status_code == 504


@pytest.mark.asyncio
async def test_manual_tx_advance_when_auto_advance_off() -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    fake.set_tx_auto_advance(False)
    async with httpx.AsyncClient(transport=fake.transport(), base_url="http://pg") as c:
        await c.post("/api/call/answer", headers={"authorization": "Bearer t"})
        await c.post("/api/call/speak", json={"text": "hi"}, headers={"authorization": "Bearer t"})
        s = (await c.get("/api/device/status", headers={"authorization": "Bearer t"})).json()
        assert (s["tx_preparing"], s["tx_active"]) == (True, False)
        # no auto-advance: still preparing on the next poll
        s = (await c.get("/api/device/status", headers={"authorization": "Bearer t"})).json()
        assert (s["tx_preparing"], s["tx_active"]) == (True, False)
        fake.advance_tx()
        s = (await c.get("/api/device/status", headers={"authorization": "Bearer t"})).json()
        assert (s["tx_preparing"], s["tx_active"]) == (False, True)


@pytest.mark.asyncio
async def test_fake_redis_delete() -> None:
    r = FakeAsyncRedis()
    await r.set("k", "1")
    await r.delete("k")
    assert await r.get("k") is None


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
