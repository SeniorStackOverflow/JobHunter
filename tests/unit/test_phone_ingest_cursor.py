from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CommunicationSession, UserProfile
from app.models.enums import CommunicationChannel, CommunicationDirection
from app.phone.correlation import CallerCorrelation
from app.phone.health import HealthTracker
from app.phone.ingest import EVENTS_STATE_KEY, IngestLoop
from app.phone.schemas import DeviceStatus, EventsPage, PhoneEvent
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
    assert json.loads(await redis.get(EVENTS_STATE_KEY))["cursor"] == 9
    assert await loop.load_cursor() == 9
    assert loop._cursor_seeded is True  # Still seeded after load


@pytest.mark.asyncio
async def test_is_reset_requires_empty_page_below_cursor(
    sqlite_session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """F1 review / HIGH #1: a page that still carries events at/above the cursor
    is never a reset, even if its reported latest_id is low (malformed page)."""
    loop = _loop(sqlite_session_factory, redis)
    loop._cursor = 100

    # genuine reset: nothing returned from after_id=cursor, reported max below it
    assert loop._is_reset(EventsPage(events=[], latest_id=5)) is True
    # malformed/partial page: latest_id looks like a reset but events are present
    assert (
        loop._is_reset(EventsPage(events=[PhoneEvent(id=101, type="call_state")], latest_id=0))
        is False
    )
    # healthy page
    assert (
        loop._is_reset(EventsPage(events=[PhoneEvent(id=101, type="call_state")], latest_id=101))
        is False
    )


@pytest.mark.asyncio
async def test_detect_reset_considers_both_boot_ids(
    sqlite_session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """F1 review round 4 / HIGH: /device/status and /events are two calls a moment
    apart. A restart between them yields two different boot ids in one cycle and
    must still be detected — picking one and ignoring the other misses it."""
    loop = _loop(sqlite_session_factory, redis)
    loop._cursor = 100

    def status(boot: str, state: str = "IDLE") -> DeviceStatus:
        return DeviceStatus(boot_id=boot, call_state=state, latest_event_id=100)

    def page(boot: str) -> EventsPage:
        return EventsPage(events=[], latest_id=100, boot_id=boot)

    # stored boot A; a fresh (post-restart) boot B on EITHER feed is a reset,
    # even though the id heuristic here would say "not a reset" (latest_id == cursor)
    loop._boot_id = "A"
    assert loop._detect_reset(status("A"), page("A")) is False
    assert loop._detect_reset(status("A"), page("B")) is True  # restart mid-cycle
    assert loop._detect_reset(status("B"), page("B")) is True
    assert loop._detect_reset(status("B"), page("A")) is True

    # no stored boot yet: only a mid-cycle disagreement counts as a reset
    loop._boot_id = ""
    assert loop._detect_reset(status("A"), page("A")) is False
    assert loop._detect_reset(status("A"), page("B")) is True


@pytest.mark.asyncio
async def test_seed_state_persists_boot_id_atomically(
    sqlite_session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """F1 review round 4 / BLOCKER: the first-start seed must persist the boot id
    in the same write as the cursor, or a restart in the startup window (before
    the boot id is recorded on a later cycle) is invisible."""
    loop = _loop(sqlite_session_factory, redis)
    await loop.seed_state(5, "boot-A")

    stored = json.loads(await redis.get(EVENTS_STATE_KEY))
    assert stored == {"cursor": 5, "generation": 0, "boot_id": "boot-A"}

    # a fresh loop (== restarted agent) loads the boot id and now sees a restart
    reloaded = _loop(sqlite_session_factory, redis)
    assert await reloaded.load_cursor() == 5
    assert reloaded._boot_id == "boot-A"
    assert (
        reloaded._detect_reset(
            DeviceStatus(boot_id="boot-B", latest_event_id=5),
            EventsPage(events=[], latest_id=5, boot_id="boot-B"),
        )
        is True
    )


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
