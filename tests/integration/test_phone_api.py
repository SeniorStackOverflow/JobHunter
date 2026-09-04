from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from app.database import get_session
from app.main import app
from app.models.entities import (
    CommunicationSession,
    CommunicationTurn,
    PhoneChannelHealth,
    PhoneDeviceSnapshot,
    UserProfile,
)
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    PhoneComponentStatus,
    TurnSpeaker,
)
from tests.fixtures.fake_redis import FakeAsyncRedis


@pytest_asyncio.fixture
async def client(sqlite_session_factory, monkeypatch) -> httpx.AsyncClient:
    async def _override():
        async with sqlite_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    monkeypatch.setattr("app.api.dependencies.get_settings", lambda: _settings_with_key())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"Authorization": "Bearer secret"}
    ) as c:
        yield c
    app.dependency_overrides.clear()


def _settings_with_key():
    from app.security.auth import hash_api_key
    from app.settings.config import Settings

    return Settings(_env_file=None, mcp_api_keys_hashed=[hash_api_key("secret")])


@pytest.fixture(autouse=True)
def _fake_phone_routes_redis(monkeypatch) -> FakeAsyncRedis:
    """`/status` opens a short-lived async Redis connection; keep the suite hermetic."""
    fake = FakeAsyncRedis()

    class _RedisModule:
        @staticmethod
        def from_url(*args: object, **kwargs: object) -> FakeAsyncRedis:
            return fake

    monkeypatch.setattr("app.api.phone_routes.AsyncRedis", _RedisModule)
    return fake


@pytest.mark.asyncio
async def test_status_endpoint_reports_channel(client, sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        session.add(
            PhoneChannelHealth(
                component="phonegate_transport",
                status=PhoneComponentStatus.HEALTHY,
                updated_at=datetime.now(UTC),
            )
        )
        session.add(
            PhoneChannelHealth(
                component="a14_daemon",
                status=PhoneComponentStatus.DEGRADED,
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()

    body = (await client.get("/api/v1/phone/status")).json()
    assert body["channel"] == "degraded"
    assert {c["component"] for c in body["components"]} >= {"phonegate_transport", "a14_daemon"}


@pytest.mark.asyncio
async def test_status_downgrades_stale_agent(client, sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        session.add(
            PhoneChannelHealth(
                component="phonegate_transport",
                status=PhoneComponentStatus.HEALTHY,
                last_ok_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        session.add(
            PhoneChannelHealth(
                component="agent",
                status=PhoneComponentStatus.HEALTHY,
                last_ok_at=datetime.now(UTC) - timedelta(hours=1),
                updated_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await session.commit()

    body = (await client.get("/api/v1/phone/status")).json()
    agent_component = next(c for c in body["components"] if c["component"] == "agent")
    assert agent_component["status"] == "unavailable"
    assert body["agent"]["status"] == "unavailable"
    assert body["agent"]["stale"] is True
    assert body["channel"] == "unavailable"


@pytest.mark.asyncio
async def test_status_keeps_fresh_agent_healthy(client, sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        session.add(
            PhoneChannelHealth(
                component="agent",
                status=PhoneComponentStatus.HEALTHY,
                last_ok_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()

    body = (await client.get("/api/v1/phone/status")).json()
    assert body["agent"]["status"] == "healthy"
    assert body["agent"]["stale"] is False


@pytest.mark.asyncio
async def test_sessions_list_and_detail(client, sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="d", is_default=True)
        session.add(profile)
        await session.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address="+37360111222",
            remote_raw="+37360111222",
            phonegate_event_id_start=1,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            outcome=CommunicationOutcome.COMPLETED,
        )
        session.add(call)
        await session.flush()
        session.add(
            CommunicationTurn(
                session_id=call.id,
                phonegate_transcript_id=1,
                seq=1,
                speaker=TurnSpeaker.EMPLOYER,
                text="hi",
                occurred_at=datetime.now(UTC),
            )
        )
        await session.commit()
        call_id = call.id

    listing = (await client.get("/api/v1/phone/sessions")).json()
    assert listing["sessions"][0]["remote_address"] == "+373••••222"

    detail = (await client.get(f"/api/v1/phone/sessions/{call_id}")).json()
    assert detail["turns"][0]["text"] == "hi"


@pytest.mark.asyncio
async def test_status_device_block_and_idle_call(client, sqlite_session_factory) -> None:
    """Test device snapshot appears in /status and idle call state."""
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="d", is_default=True)
        session.add(profile)
        await session.flush()

        # Add a device snapshot
        device_snap = PhoneDeviceSnapshot(
            id="current",
            payload={
                "daemon_version": "0.2.1",
                "battery": 87,
                "sim_operator": "Orange",
                "rx_audio_stats": {"captured_frames": 0, "queued_frames": 0, "dropped_frames": 0},
            },
            updated_at=datetime.now(UTC),
        )
        session.add(device_snap)
        await session.flush()

        # Add an ended call
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address="+37360111222",
            remote_raw="+37360111222",
            phonegate_event_id_start=1,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            outcome=CommunicationOutcome.COMPLETED,
        )
        session.add(call)
        await session.commit()

    body = (await client.get("/api/v1/phone/status")).json()

    # Verify device block is populated
    assert body["device"]["daemon_version"] == "0.2.1"
    assert body["device"]["battery"] == 87
    assert body["device"]["sim_operator"] == "Orange"
    assert "rx_audio_stats" in body["device"]
    # F4b: the snapshot's own updated_at column surfaces as an ISO timestamp
    assert "updated_at" in body["device"]
    datetime.fromisoformat(body["device"]["updated_at"])

    # Verify current_call is idle since session ended
    assert body["current_call"]["state"] == "idle"
    assert body["current_call"]["session_id"] is None
    assert body["current_call"]["caller_number"] is None


@pytest.mark.asyncio
async def test_status_auto_answer_block(client, sqlite_session_factory) -> None:
    async with sqlite_session_factory() as s:
        profile = UserProfile(name="d", is_default=True)
        s.add(profile)
        await s.flush()
        s.add(
            CommunicationSession(
                profile_id=profile.id,
                channel=CommunicationChannel.CALL,
                transport="phonegate",
                direction=CommunicationDirection.INBOUND,
                remote_address="+37360111222",
                remote_raw="+37360111222",
                phonegate_event_id_start=1,
                started_at=datetime.now(UTC),
                answered_at=datetime.now(UTC),
                auto_answered=True,
                script_stage="listening",
            )
        )
        await s.commit()

    body = (await client.get("/api/v1/phone/status")).json()
    assert body["auto_answer"]["enabled"] in (True, False)
    assert body["auto_answer"]["stopped"] is False
    assert body["current_call"]["auto_answered"] is True
    assert body["current_call"]["script_stage"] == "listening"


@pytest.mark.asyncio
async def test_status_requires_auth(sqlite_session_factory) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/api/v1/phone/status")).status_code == 401
