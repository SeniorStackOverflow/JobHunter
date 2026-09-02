from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CommunicationSession, UserProfile
from app.models.enums import CommunicationChannel, CommunicationDirection
from app.phone.correlation import CallerCorrelation
from app.phone.health import HealthTracker
from app.phone.ingest import EVENTS_CURSOR_KEY, IngestLoop
from app.phone.schemas import DeviceStatus
from app.phone.sessions import SessionStore
from app.settings.config import Settings
from tests.fixtures.fake_redis import FakeAsyncRedis


def _loop(factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis) -> IngestLoop:
    return IngestLoop(
        client=None,  # type: ignore[arg-type]  # not used by the methods under test
        session_factory=factory,
        redis=redis,  # type: ignore[arg-type]
        correlation=CallerCorrelation(),
        health=HealthTracker(),
        settings=Settings(_env_file=None),
    )


@pytest_asyncio.fixture
async def redis() -> FakeAsyncRedis:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.mark.asyncio
async def test_load_cursor_reports_absent_key_as_none(
    sqlite_session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    loop = _loop(sqlite_session_factory, redis)
    # Absent key -> None (caller seeds from latest_event_id), internal cursor 0.
    # A1: _cursor_seeded should remain False when key is absent
    assert loop._cursor_seeded is False
    assert await loop.load_cursor() is None
    assert loop._cursor == 0
    assert loop._cursor_seeded is False  # Still not seeded
    await loop.save_cursor(9)
    assert loop._cursor_seeded is True  # Now seeded after save
    assert await redis.get(EVENTS_CURSOR_KEY) == "9"
    assert await loop.load_cursor() == 9
    assert loop._cursor_seeded is True  # Still seeded after load


@pytest.mark.asyncio
async def test_reconcile_closes_dangling_open_session_when_idle(
    sqlite_session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    async with sqlite_session_factory() as session:
        p = UserProfile(name="d", is_default=True)
        session.add(p)
        await session.flush()
        session.add(
            CommunicationSession(
                profile_id=p.id,
                channel=CommunicationChannel.CALL,
                transport="phonegate",
                direction=CommunicationDirection.INBOUND,
                remote_address="",
                remote_raw="",
                phonegate_event_id_start=1,
                started_at=datetime.now(UTC),
            )
        )
        await session.commit()

    loop = _loop(sqlite_session_factory, redis)
    await loop.reconcile(
        DeviceStatus.model_validate(
            {
                "connected": True,
                "mode": "Zero-ADB",
                "call_state": "IDLE",
                "rx_audio_stats": {},
                "device": {},
            }
        )
    )

    async with sqlite_session_factory() as session:
        row = await SessionStore().find_open(session)
    assert row is None
