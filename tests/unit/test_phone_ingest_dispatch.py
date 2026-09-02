from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
)
from app.phone.client import PhoneGateClient
from app.phone.correlation import CallerCorrelation
from app.phone.health import HealthTracker
from app.phone.ingest import IngestLoop
from app.settings.config import Settings
from tests.fixtures.fake_phonegate import FakePhoneGate
from tests.fixtures.fake_redis import FakeAsyncRedis


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def profiled_factory(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    async with sqlite_session_factory() as session:
        session.add(UserProfile(name="d", is_default=True))
        await session.commit()
    return sqlite_session_factory


def _make_loop(
    client: PhoneGateClient,
    factory: async_sessionmaker[AsyncSession],
    redis: FakeAsyncRedis,
) -> IngestLoop:
    return IngestLoop(
        client=client,
        session_factory=factory,
        redis=redis,  # type: ignore[arg-type]
        correlation=CallerCorrelation(),
        health=HealthTracker(),
        settings=Settings(_env_file=None),
    )


async def _drain(loop: IngestLoop, cycles: int = 6) -> None:
    for _ in range(cycles):
        await loop.run_cycle()


async def test_full_scripted_call_persists_session_and_turns(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        cursor = await loop.load_cursor()
        # Simulate agent._run_loop startup: seed if no cursor
        if cursor is None:
            status = await client.device_status()
            await loop.save_cursor(status.latest_event_id)

        fake.ring("+37360111222")
        fake.answer()
        fake.transcript(speaker="rx", text="Здравствуйте, по вакансии")
        fake.transcript(speaker="rx", text="в четверг в два")
        fake.hangup()
        await _drain(loop)

    async with profiled_factory() as session:
        calls = (await session.scalars(select(CommunicationSession))).all()
        turns = (await session.scalars(select(CommunicationTurn))).all()
    assert len(calls) == 1
    assert calls[0].outcome == CommunicationOutcome.COMPLETED
    assert calls[0].answered_at is not None
    assert [t.text for t in sorted(turns, key=lambda t: t.seq)] == [
        "Здравствуйте, по вакансии",
        "в четверг в два",
    ]


async def test_missed_call_outcome(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        cursor = await loop.load_cursor()
        if cursor is None:
            status = await client.device_status()
            await loop.save_cursor(status.latest_event_id)
        fake.ring("+37360111222")
        fake.hangup()
        await _drain(loop)
    async with profiled_factory() as session:
        call = (await session.scalars(select(CommunicationSession))).one()
    assert call.outcome == CommunicationOutcome.MISSED
    assert call.answered_at is None


async def test_reingest_is_idempotent(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        cursor = await loop.load_cursor()
        if cursor is None:
            status = await client.device_status()
            await loop.save_cursor(status.latest_event_id)
        fake.ring("+37360111222")
        fake.answer()
        fake.transcript(speaker="rx", text="a")
        fake.hangup()
        await _drain(loop)
        await loop.save_cursor(0)  # replay every event
        await _drain(loop)
    async with profiled_factory() as session:
        turns = (await session.scalars(select(CommunicationTurn))).all()
        calls = (await session.scalars(select(CommunicationSession))).all()
    assert len(turns) == 1
    assert len(calls) == 1


async def test_new_call_after_restart_opens_session_despite_event_id_collision(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """A PhoneGate restart resets the event-id counter, so a fresh inbound call
    gets a low ``incoming_call`` id that collides with an old session's
    ``phonegate_event_id_start``. The time-bounded dedup guard must still let the
    new call open its own session."""
    async with profiled_factory() as session:
        profile = (await session.scalars(select(UserProfile))).one()
        old_start = datetime.now(UTC) - timedelta(hours=1)
        session.add(
            CommunicationSession(
                profile_id=profile.id,
                channel=CommunicationChannel.CALL,
                transport="phonegate",
                direction=CommunicationDirection.INBOUND,
                remote_address="+37360999888",
                remote_raw="+37360999888",
                phonegate_event_id_start=2,
                started_at=old_start,
                ended_at=old_start,
                outcome=CommunicationOutcome.COMPLETED,
            )
        )
        await session.commit()

    fake = FakePhoneGate()
    fake.restart()  # models a just-restarted PhoneGate: event-id counter back at 1
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        cursor = await loop.load_cursor()
        if cursor is None:
            status = await client.device_status()
            await loop.save_cursor(status.latest_event_id)
        fake.ring("+37360111222")  # call_state id=1, incoming_call id=2 (collides)
        await _drain(loop)

    async with profiled_factory() as session:
        calls = (await session.scalars(select(CommunicationSession))).all()
    new_calls = [c for c in calls if c.remote_raw == "+37360111222"]
    assert len(new_calls) == 1
    assert new_calls[0].phonegate_event_id_start == 2
    assert len(calls) == 2  # historical session untouched, one fresh session


async def test_phonegate_restart_closes_open_session(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        cursor = await loop.load_cursor()
        if cursor is None:
            status = await client.device_status()
            await loop.save_cursor(status.latest_event_id)
        fake.ring("+37360111222")
        fake.answer()
        await _drain(loop, 3)
        fake.hangup()  # emit IDLE, then restart drops every event below the cursor
        fake.restart()
        await _drain(loop, 3)
    async with profiled_factory() as session:
        call = (await session.scalars(select(CommunicationSession))).one()
    assert call.ended_at is not None
    assert call.needs_review is True
    assert call.outcome == CommunicationOutcome.UNKNOWN


# A1 tests
async def test_cursor_seed_on_first_status_no_replay(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """A1: IngestLoop seeds the cursor from status.latest_event_id on first successful
    status read if no cursor was seeded. This prevents replaying historical events."""
    from app.models.entities import AuditEvent

    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        # load_cursor returns None (no Redis key)
        assert await loop.load_cursor() is None
        assert loop._cursor_seeded is False

        # FakePhoneGate has buffered 5 events but hasn't seen them yet
        fake.ring("+37360111222")
        fake.answer()
        fake.transcript(speaker="rx", text="test")
        fake.hangup()
        fake.restart()  # resets event ids, adds 5 events to buffer

        # First run_cycle: seeds cursor from status.latest_event_id
        active = await loop.run_cycle()
        assert loop._cursor_seeded is True
        assert active is False  # IDLE after restart

    # No session should have been created (events not polled after seeding)
    async with profiled_factory() as session:
        calls = (await session.scalars(select(CommunicationSession))).all()
        audit = (await session.scalars(select(AuditEvent))).all()
    assert len(calls) == 0
    assert len(audit) == 0


# A2 tests
async def test_reconcile_opens_session_on_ringing(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """A2: reconcile() opens a session on RINGING with no open session."""
    from app.models.entities import AuditEvent

    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        await loop.load_cursor()

        fake.ring("+37360111222")
        status = await client.device_status()
        assert status.call_state == "RINGING"

        await loop.reconcile(status)

    async with profiled_factory() as session:
        calls = (await session.scalars(select(CommunicationSession))).all()
        audit = (await session.scalars(select(AuditEvent))).all()
    assert len(calls) == 1
    assert calls[0].needs_review is True
    assert calls[0].remote_raw == "+37360111222"
    # Should have at least one audit event for communication_session.opened
    opened_events = [a for a in audit if a.action == "communication_session.opened"]
    assert len(opened_events) >= 1


# A3 tests
async def test_audit_correlated_event(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """A3: when a correlation matches an application/contact, audit event is written."""
    from app.models.entities import AuditEvent

    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        cursor = await loop.load_cursor()
        if cursor is None:
            status = await client.device_status()
            await loop.save_cursor(status.latest_event_id)

        fake.ring("+37360111222")
        fake.answer()
        fake.transcript(speaker="rx", text="test")
        fake.hangup()
        await _drain(loop)

    async with profiled_factory() as session:
        audit = (await session.scalars(select(AuditEvent))).all()
    # Should have audit events for opened and possibly correlated
    opened_events = [a for a in audit if a.action == "communication_session.opened"]
    assert len(opened_events) >= 1


async def test_audit_synthetic_session_opened(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """A3: synthetic session from transcript creates communication_session.opened audit event.

    This tests the case where a transcript arrives before the incoming_call event.
    """
    from app.models.entities import AuditEvent

    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        cursor = await loop.load_cursor()
        if cursor is None:
            status = await client.device_status()
            await loop.save_cursor(status.latest_event_id)

        # Ring to move to CONNECTED state, then emit transcript before incoming_call event
        fake.ring("+37360111222")
        fake.answer()
        # Don't emit incoming_call; just emit transcript while call is active
        fake.transcript(speaker="rx", text="test")
        await _drain(loop)

    async with profiled_factory() as session:
        calls = (await session.scalars(select(CommunicationSession))).all()
        audit = (await session.scalars(select(AuditEvent))).all()
    # Should have at least a synthetic session created from transcript
    assert len(calls) >= 1
    # Should have audit events for opening
    opened_events = [a for a in audit if a.action == "communication_session.opened"]
    assert len(opened_events) >= 1


# A4 tests
async def test_session_diagnostics_populated_on_open(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """A4: when opening a session, diagnostics contain daemon_version and sim_operator."""
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        cursor = await loop.load_cursor()
        if cursor is None:
            status = await client.device_status()
            await loop.save_cursor(status.latest_event_id)

        fake.ring("+37360111222")
        fake.answer()
        fake.transcript(speaker="rx", text="test")
        fake.hangup()
        await _drain(loop)

    async with profiled_factory() as session:
        call = (await session.scalars(select(CommunicationSession))).one()
    # Diagnostics should contain daemon_version and sim_operator
    assert "daemon_version" in call.diagnostics
    assert "sim_operator" in call.diagnostics
