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
    UserProfile,
)
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    PhoneComponentStatus,
    TurnSpeaker,
)


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
async def test_status_requires_auth(sqlite_session_factory) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/api/v1/phone/status")).status_code == 401
