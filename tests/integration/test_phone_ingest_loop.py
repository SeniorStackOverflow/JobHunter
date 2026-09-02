from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import (
    AuditEvent,
    CallFact,
    CommunicationSession,
    CommunicationTurn,
    InterviewAppointment,
    PhoneChannelHealth,
    UserProfile,
)
from app.models.enums import CommunicationOutcome
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


def _loop(
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


async def test_agent_restart_reconciles_dangling_session(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """Test restart: ring+answer (3 cycles), restart, hangup (3 cycles)."""
    fake = FakePhoneGate()
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c1:
        loop1 = _loop(c1, profiled_factory, redis)
        cursor = await loop1.load_cursor()
        if cursor is None:
            status = await c1.device_status()
            await loop1.save_cursor(status.latest_event_id)
        fake.ring("+37360111222")
        fake.answer()
        for _ in range(3):
            await loop1.run_cycle()

    # a brand-new loop == a restarted process, sharing the same redis + session_factory
    async with PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()) as c2:
        loop2 = _loop(c2, profiled_factory, redis)
        cursor = await loop2.load_cursor()
        if cursor is None:
            status = await c2.device_status()
            await loop2.save_cursor(status.latest_event_id)
        # Exercise reconcile path end-to-end
        status = await c2.device_status()
        await loop2.reconcile(status)
        fake.hangup()
        for _ in range(3):
            await loop2.run_cycle()

    async with profiled_factory() as session:
        call = (await session.scalars(select(CommunicationSession))).one()
        audits = (await session.scalars(select(AuditEvent))).all()
        health = (await session.scalars(select(PhoneChannelHealth))).all()

    # assertions on persisted call state
    assert call.ended_at is not None
    assert call.outcome in {CommunicationOutcome.COMPLETED, CommunicationOutcome.UNKNOWN}
    # audit: at least one "session opened" event
    assert any(a.action == "communication_session.opened" for a in audits)
    # health: components tracked during the call
    assert {h.component for h in health} >= {"phonegate_transport", "a14_daemon", "agent"}


async def test_full_call_persists_complete_graph(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """Verify full integration: sessions, turns, audit, health rows persisted correctly."""
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _loop(client, profiled_factory, redis)
        cursor = await loop.load_cursor()
        if cursor is None:
            status = await client.device_status()
            await loop.save_cursor(status.latest_event_id)

        fake.ring("+37360111222")
        fake.answer()
        fake.transcript(speaker="rx", text="Здравствуйте, по вакансии")
        fake.transcript(speaker="rx", text="в четверг в два")
        fake.hangup()
        for _ in range(6):
            await loop.run_cycle()

    async with profiled_factory() as session:
        sessions = (await session.scalars(select(CommunicationSession))).all()
        turns = (await session.scalars(select(CommunicationTurn))).all()
        audits = (await session.scalars(select(AuditEvent))).all()
        health = (await session.scalars(select(PhoneChannelHealth))).all()
        facts = (await session.scalars(select(CallFact))).all()
        appointments = (await session.scalars(select(InterviewAppointment))).all()

    # full graph assertions
    assert len(sessions) == 1
    assert sessions[0].outcome == CommunicationOutcome.COMPLETED
    assert sessions[0].answered_at is not None
    assert len(turns) == 2
    assert [t.text for t in sorted(turns, key=lambda t: t.seq)] == [
        "Здравствуйте, по вакансии",
        "в четверг в два",
    ]
    assert any(a.action == "communication_session.opened" for a in audits)
    assert len(health) > 0
    assert {h.component for h in health} >= {"phonegate_transport", "a14_daemon", "agent"}
    # Phase 1 does not write CallFact or InterviewAppointment, so these should be empty
    assert facts == []
    assert appointments == []
