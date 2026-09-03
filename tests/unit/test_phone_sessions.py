from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import UserProfile
from app.models.enums import CommunicationOutcome
from app.phone.correlation import CorrelationResult
from app.phone.sessions import SessionStore


@pytest_asyncio.fixture
async def db(sqlite_session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    async with sqlite_session_factory() as session:
        p = UserProfile(name="d", is_default=True)
        session.add(p)
        await session.commit()
        session.info["profile_id"] = p.id
        yield session


def _corr(profile_id: object) -> CorrelationResult:
    return CorrelationResult(profile_id, None, None, None, None)  # type: ignore[arg-type]


async def test_open_find_close(db: AsyncSession) -> None:
    store = SessionStore()
    now = datetime.now(UTC)
    call = await store.open(
        db,
        remote_raw="+37360111222",
        remote_address="+37360111222",
        event_id=3,
        correlation=_corr(db.info["profile_id"]),
        opened_at=now,
    )
    await db.commit()

    open_row = await store.find_open(db)
    assert open_row is not None and open_row.id == call.id

    await store.touch_answered(call, now)
    await store.close(db, call, outcome=CommunicationOutcome.COMPLETED, ended_at=now)
    await db.commit()

    assert await store.find_open(db) is None
    refreshed = await db.get(type(call), call.id)
    assert refreshed is not None and refreshed.outcome == CommunicationOutcome.COMPLETED
    assert refreshed.answered_at is not None


async def test_append_turn_is_idempotent(db: AsyncSession) -> None:
    from app.models.enums import TurnSpeaker
    from app.phone.schemas import TranscriptEntry
    from app.phone.sessions import SessionStore, speaker_from_phonegate

    store = SessionStore()
    now = datetime.now(UTC)
    call = await store.open(
        db,
        remote_raw="+3736011",
        remote_address="+3736011",
        event_id=1,
        correlation=_corr(db.info["profile_id"]),
        opened_at=now,
    )
    await db.flush()

    entry = TranscriptEntry(
        id=5, speaker="rx", text="Здравствуйте", confidence=0.8, backend="groq", timestamp_ms=1
    )
    first = await store.append_turn(db, session_id=call.id, entry=entry)
    assert first is not None and first.seq == 1 and first.speaker == TurnSpeaker.EMPLOYER
    second = await store.append_turn(db, session_id=call.id, entry=entry)
    assert second is None

    entry2 = TranscriptEntry(id=6, speaker="tx", text="ответ", timestamp_ms=2)
    third = await store.append_turn(db, session_id=call.id, entry=entry2)
    assert third is not None and third.seq == 2 and third.speaker == TurnSpeaker.ASSISTANT
    assert speaker_from_phonegate("weird") is TurnSpeaker.SYSTEM


async def test_open_with_diagnostics(db: AsyncSession) -> None:
    """A4: SessionStore.open accepts diagnostics parameter and merges it."""
    store = SessionStore()
    now = datetime.now(UTC)
    call = await store.open(
        db,
        remote_raw="+37360111222",
        remote_address="+37360111222",
        event_id=3,
        correlation=_corr(db.info["profile_id"]),
        opened_at=now,
        diagnostics={
            "daemon_version": "0.2.1",
            "sim_operator": "Orange",
        },
    )
    await db.commit()

    refreshed = await db.get(type(call), call.id)
    assert refreshed is not None
    assert refreshed.diagnostics.get("daemon_version") == "0.2.1"
    assert refreshed.diagnostics.get("sim_operator") == "Orange"


async def test_open_stamps_generation_and_answered_at(db: AsyncSession) -> None:
    """F1: SessionStore.open records the PhoneGate generation and an optional
    answered_at (used when reconcile opens a session for an already-active call)."""
    store = SessionStore()
    now = datetime.now(UTC)
    call = await store.open(
        db,
        remote_raw="+37360111222",
        remote_address="+37360111222",
        event_id=0,
        correlation=_corr(db.info["profile_id"]),
        opened_at=now,
        generation=3,
        answered_at=now,
    )
    await db.commit()

    refreshed = await db.get(type(call), call.id)
    assert refreshed is not None
    assert refreshed.phonegate_generation == 3
    assert refreshed.answered_at is not None


async def test_open_defaults_generation_to_zero(db: AsyncSession) -> None:
    store = SessionStore()
    call = await store.open(
        db,
        remote_raw="+3736011",
        remote_address="+3736011",
        event_id=1,
        correlation=_corr(db.info["profile_id"]),
        opened_at=datetime.now(UTC),
    )
    await db.commit()
    refreshed = await db.get(type(call), call.id)
    assert refreshed is not None
    assert refreshed.phonegate_generation == 0
    assert refreshed.answered_at is None


async def test_open_with_diagnostics_and_note(db: AsyncSession) -> None:
    """A4: when both diagnostics and note are provided, both are merged."""
    store = SessionStore()
    now = datetime.now(UTC)
    call = await store.open(
        db,
        remote_raw="+37360111222",
        remote_address="+37360111222",
        event_id=3,
        correlation=_corr(db.info["profile_id"]),
        opened_at=now,
        note="test_note",
        diagnostics={
            "daemon_version": "0.2.1",
            "sim_operator": "Orange",
        },
    )
    await db.commit()

    refreshed = await db.get(type(call), call.id)
    assert refreshed is not None
    assert refreshed.diagnostics.get("note") == "test_note"
    assert refreshed.diagnostics.get("daemon_version") == "0.2.1"
    assert refreshed.diagnostics.get("sim_operator") == "Orange"


async def test_record_assistant_turn_and_delivery(db: AsyncSession) -> None:
    from app.models.enums import TurnDeliveryStatus, TurnSpeaker

    store = SessionStore()
    now = datetime.now(UTC)
    call = await store.open(
        db,
        remote_raw="+3736011",
        remote_address="+3736011",
        event_id=1,
        correlation=_corr(db.info["profile_id"]),
        opened_at=now,
    )
    await db.flush()
    turn = await store.record_assistant_turn(
        db,
        session_id=call.id,
        phonegate_transcript_id=7,
        spoken_text="Здравствуйте",
        delivery_status=TurnDeliveryStatus.ATTEMPTED,
        occurred_at=now,
    )
    assert turn.speaker is TurnSpeaker.ASSISTANT
    assert turn.spoken_text == "Здравствуйте"
    assert turn.seq == 1
    await store.set_turn_delivery(db, turn_id=turn.id, status=TurnDeliveryStatus.DELIVERED)
    await db.commit()
    refreshed = await db.get(type(turn), turn.id)
    assert refreshed is not None and refreshed.delivery_status is TurnDeliveryStatus.DELIVERED


async def test_set_script_stage_and_mark_auto_answered(db: AsyncSession) -> None:
    store = SessionStore()
    now = datetime.now(UTC)
    call = await store.open(
        db,
        remote_raw="+3736011",
        remote_address="+3736011",
        event_id=1,
        correlation=_corr(db.info["profile_id"]),
        opened_at=now,
    )
    await store.mark_auto_answered(call, now)
    await store.set_script_stage(call, "greeting")
    await db.commit()
    refreshed = await db.get(type(call), call.id)
    assert refreshed is not None
    assert refreshed.auto_answered is True
    assert refreshed.answered_at is not None
    assert refreshed.script_stage == "greeting"


def test_speaker_from_phonegate_tx_is_assistant() -> None:
    from app.models.enums import TurnSpeaker
    from app.phone.sessions import speaker_from_phonegate

    assert speaker_from_phonegate("tx") is TurnSpeaker.ASSISTANT
