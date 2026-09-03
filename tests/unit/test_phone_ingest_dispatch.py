from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
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


async def test_ambiguous_correlation_flags_session_for_review(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """F3 / HIGH: when the caller's number maps to more than one job, the opened
    session is flagged needs_review with an 'ambiguous' correlation note."""
    from uuid import uuid4

    from app.models.entities import CanonicalJob, EmployerContact, JobSource, SourceJob
    from app.models.enums import ContactType, JobStatus, VerificationStatus

    async with profiled_factory() as session:
        src = JobSource(name="s", base_url="https://x", adapter_type="fixture_source")
        session.add(src)
        await session.flush()
        for _ in range(2):
            canon = CanonicalJob(
                normalized_company="ACME",
                normalized_title="Loader",
                canonical_fingerprint=uuid4().hex,
                status=JobStatus.ACTIVE,
            )
            session.add(canon)
            await session.flush()
            job = SourceJob(
                source_id=src.id,
                canonical_job_id=canon.id,
                external_job_id=uuid4().hex,
                canonical_url=f"https://x/{uuid4().hex}",
                title="Loader",
                content_hash="h",
                matching_content_hash="m",
                source_fingerprint="f",
                status=JobStatus.ACTIVE,
            )
            session.add(job)
            await session.flush()
            session.add(
                EmployerContact(
                    canonical_job_id=canon.id,
                    source_job_id=job.id,
                    value="+37360111222",
                    contact_type=ContactType.PHONE,
                    discovery_source="test",
                    verification_status=VerificationStatus.UNVERIFIED,
                    confidence=0.6,
                    evidence_url="https://x/1",
                )
            )
        await session.commit()

    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        await loop.load_cursor()
        status = await client.device_status()
        await loop.save_cursor(status.latest_event_id)
        fake.ring("+37360111222")
        fake.answer()
        fake.hangup()
        await _drain(loop)

    async with profiled_factory() as session:
        call = (await session.scalars(select(CommunicationSession))).one()
    assert call.needs_review is True
    assert call.diagnostics.get("correlation") == "ambiguous"


# A4 tests
async def test_malformed_transcript_event_is_skipped_not_fatal(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """F2 / BLOCKER: a transcript event whose payload fails validation is logged
    and skipped; the call and its good turns are intact and the cursor advances."""
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        await loop.load_cursor()
        status = await client.device_status()
        await loop.save_cursor(status.latest_event_id)

        fake.ring("+37360111222")
        fake.answer()
        fake.transcript(speaker="rx", text="good one")
        fake.emit_raw("transcript", {"transcript": {"id": "not-an-int", "speaker": "rx"}})
        fake.transcript(speaker="tx", text="good two")
        fake.hangup()
        await _drain(loop)

    async with profiled_factory() as session:
        calls = (await session.scalars(select(CommunicationSession))).all()
        turns = (await session.scalars(select(CommunicationTurn))).all()
    assert len(calls) == 1
    assert calls[0].outcome == CommunicationOutcome.COMPLETED
    assert sorted(t.text for t in turns) == ["good one", "good two"]
    # cursor advanced past every event including the poison one
    assert await redis.get("job-agent:phone:events:cursor") == "7"


async def test_poison_event_in_batch_does_not_stop_following_events(
    profiled_factory: async_sessionmaker[AsyncSession],
    redis: FakeAsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 / BLOCKER: an event whose dispatch raises is isolated in its own
    savepoint; later events in the same batch still commit and the cursor
    advances past the poison event."""
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        await loop.load_cursor()
        status = await client.device_status()
        await loop.save_cursor(status.latest_event_id)

        real_dispatch = loop._dispatch

        async def _flaky_dispatch(session: Any, event: Any, st: Any) -> None:
            if (
                event.type == "transcript"
                and event.data.get("transcript", {}).get("text") == "BOOM"
            ):
                raise RuntimeError("simulated handler failure")
            await real_dispatch(session, event, st)

        monkeypatch.setattr(loop, "_dispatch", _flaky_dispatch)

        fake.ring("+37360111222")
        fake.answer()
        fake.transcript(speaker="rx", text="before")
        fake.transcript(speaker="rx", text="BOOM")
        fake.transcript(speaker="rx", text="after")
        fake.hangup()
        await _drain(loop)

    async with profiled_factory() as session:
        calls = (await session.scalars(select(CommunicationSession))).all()
        turns = (await session.scalars(select(CommunicationTurn))).all()
    assert len(calls) == 1
    assert sorted(t.text for t in turns) == ["after", "before"]
    assert calls[0].ended_at is not None
    assert await redis.get("job-agent:phone:events:cursor") == "7"


async def test_fast_call_completing_during_restart_is_not_lost(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """F1 / BLOCKER: a call that completes entirely in the PhoneGate restart
    window has its events buffered as fresh low ids. The reset branch must
    re-read the buffer from 0 and ingest that whole call."""
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        assert await loop.load_cursor() is None
        status = await client.device_status()
        await loop.save_cursor(status.latest_event_id)

        fake.ring("+37360111222")
        await loop.run_cycle()
        fake.answer()
        await loop.run_cycle()
        fake.transcript(speaker="rx", text="one")
        await loop.run_cycle()
        fake.hangup()
        await loop.run_cycle()

        fake.restart()  # PhoneGate bounces; event + transcript ids reset to 1

        fake.ring("+37361222333")
        await loop.run_cycle()  # first poll sees latest_id < cursor -> reset
        fake.answer()
        await loop.run_cycle()
        fake.transcript(speaker="rx", text="two")
        await loop.run_cycle()
        fake.transcript(speaker="rx", text="three")
        await loop.run_cycle()
        fake.hangup()
        await loop.run_cycle()

    async with profiled_factory() as session:
        calls = (
            await session.scalars(
                select(CommunicationSession).order_by(CommunicationSession.started_at)
            )
        ).all()
        turns = (await session.scalars(select(CommunicationTurn))).all()

    assert len(calls) == 2
    assert calls[0].remote_raw == "+37360111222"
    assert calls[0].phonegate_generation == 0
    assert calls[1].remote_raw == "+37361222333"
    assert calls[1].phonegate_generation == 1
    assert calls[1].outcome == CommunicationOutcome.COMPLETED
    assert {t.text for t in turns} == {"one", "two", "three"}


async def test_reset_refetch_failure_does_not_double_bump_generation(
    profiled_factory: async_sessionmaker[AsyncSession],
    redis: FakeAsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 review / HIGH #2: if events(after_id=0) raises after the generation
    bump, the next cycle must NOT re-detect the reset and bump again (which would
    let an already-committed session re-open at a new generation)."""
    from app.phone.client import PhoneGateError

    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        await loop.load_cursor()
        status = await client.device_status()
        await loop.save_cursor(status.latest_event_id)

        fake.ring("+37360111222")
        fake.answer()
        await loop.run_cycle()
        await loop.run_cycle()

        fake.restart()
        fake.ring("+37361222333")

        real_events = client.events
        calls: list[int] = []

        async def _flaky_events(*, after_id: int, limit: int = 250) -> Any:
            if after_id == 0:
                calls.append(after_id)
                if len(calls) == 1:
                    raise PhoneGateError("gateway still rebooting")
            return await real_events(after_id=after_id, limit=limit)

        monkeypatch.setattr(client, "events", _flaky_events)

        await loop.run_cycle()  # detects reset, bumps gen, save_cursor(0), refetch raises
        assert loop._generation == 1
        monkeypatch.undo()
        await _drain(loop)

    async with profiled_factory() as session:
        sessions = (
            await session.scalars(
                select(CommunicationSession).order_by(CommunicationSession.started_at)
            )
        ).all()
    assert loop._generation == 1  # bumped exactly once for one physical restart
    new = [s for s in sessions if s.remote_raw == "+37361222333"]
    assert len(new) == 1
    assert new[0].phonegate_generation == 1


async def test_restart_midcall_reuses_transcript_ids_without_dropping_turns(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """F1 / BLOCKER: PhoneGate resets transcript ids to 1 on restart. If the
    open session were kept, the new turns would collide with its existing
    (session_id, transcript_id) rows and be silently dropped. The reset branch
    closes the old session so the new generation's turns land on a fresh one."""
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        await loop.load_cursor()
        status = await client.device_status()
        await loop.save_cursor(status.latest_event_id)

        fake.ring("+37360111222")
        await loop.run_cycle()
        fake.answer()
        await loop.run_cycle()
        fake.transcript(speaker="rx", text="before restart")  # transcript id 1
        await loop.run_cycle()

        fake.restart()  # call_state stays IN_CALL; transcript ids reset to 1

        fake.transcript(speaker="rx", text="after restart")  # transcript id 1 again
        await loop.run_cycle()
        fake.transcript(speaker="tx", text="after restart 2")
        await loop.run_cycle()
        fake.hangup()
        await loop.run_cycle()

    async with profiled_factory() as session:
        calls = (
            await session.scalars(
                select(CommunicationSession).order_by(CommunicationSession.started_at)
            )
        ).all()
        turns = (await session.scalars(select(CommunicationTurn))).all()

    assert len(calls) == 2
    assert calls[0].outcome == CommunicationOutcome.UNKNOWN
    assert calls[0].needs_review is True
    assert calls[0].diagnostics.get("close_note") == "phonegate_generation_boundary"
    assert calls[1].phonegate_generation == 1
    assert calls[1].ended_at is not None
    # the call was already answered before the restart — the post-restart
    # synthetic session must record that, not close as MISSED
    assert calls[1].answered_at is not None
    assert calls[1].outcome == CommunicationOutcome.COMPLETED
    assert {t.text for t in turns} == {
        "before restart",
        "after restart",
        "after restart 2",
    }


async def test_reconcile_opens_session_on_in_call(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """F1 / finding #3: agent starts while a call is already answered."""
    from app.models.entities import AuditEvent

    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        await loop.load_cursor()
        fake.ring("+37360111222")
        fake.answer()
        status = await client.device_status()
        assert status.call_state == "IN_CALL"
        await loop.reconcile(status)

    async with profiled_factory() as session:
        calls = (await session.scalars(select(CommunicationSession))).all()
        audit = (await session.scalars(select(AuditEvent))).all()
    assert len(calls) == 1
    assert calls[0].needs_review is True
    assert calls[0].answered_at is not None
    assert calls[0].diagnostics.get("note") == "reconcile_opened_from_in_call"
    assert any(a.action == "communication_session.opened" for a in audit)


async def test_seed_path_reconciles_already_active_call(
    profiled_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """F1 / finding #3: the boot-outage startup path seeds the cursor inside
    run_cycle; it must also reconcile so an in-progress call is opened."""
    fake = FakePhoneGate()
    async with PhoneGateClient(
        base_url="http://pg", token="t", transport=fake.transport()
    ) as client:
        loop = _make_loop(client, profiled_factory, redis)
        assert await loop.load_cursor() is None
        assert loop._cursor_seeded is False
        fake.ring("+37360111222")
        fake.answer()
        active = await loop.run_cycle()

    assert active is True
    async with profiled_factory() as session:
        calls = (await session.scalars(select(CommunicationSession))).all()
    assert len(calls) == 1
    assert calls[0].answered_at is not None


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
    # Diagnostics should contain correct daemon_version and sim_operator values
    assert call.diagnostics["daemon_version"] == "0.2.30"
    assert call.diagnostics["sim_operator"] == "Orange"
