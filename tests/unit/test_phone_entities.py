from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import (
    CommunicationSession,
    CommunicationTurn,
    PhoneChannelHealth,
    UserProfile,
)
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    PhoneComponentStatus,
    TurnSpeaker,
)


@pytest_asyncio.fixture
async def db(sqlite_session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    async with sqlite_session_factory() as session:
        yield session


async def test_session_and_turn_roundtrip(db: AsyncSession) -> None:
    profile = UserProfile(name="Основной", is_default=True)
    db.add(profile)
    await db.flush()

    call = CommunicationSession(
        profile_id=profile.id,
        channel=CommunicationChannel.CALL,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        remote_address="+37360111222",
        remote_raw="+37360111222",
        phonegate_event_id_start=5,
        started_at=datetime.now(UTC),
    )
    db.add(call)
    await db.flush()

    db.add(
        CommunicationTurn(
            session_id=call.id,
            phonegate_transcript_id=1,
            seq=1,
            speaker=TurnSpeaker.EMPLOYER,
            text="Здравствуйте",
            occurred_at=datetime.now(UTC),
        )
    )
    await db.flush()

    health = PhoneChannelHealth(
        component="phonegate_transport",
        status=PhoneComponentStatus.HEALTHY,
        updated_at=datetime.now(UTC),
    )
    db.add(health)
    await db.commit()

    loaded = await db.get(CommunicationSession, call.id)
    assert loaded is not None
    assert loaded.direction == CommunicationDirection.INBOUND
    assert loaded.ended_at is None


async def test_turn_unique_transcript_id_per_session(db: AsyncSession) -> None:
    profile = UserProfile(name="p", is_default=True)
    db.add(profile)
    await db.flush()
    call = CommunicationSession(
        profile_id=profile.id,
        channel=CommunicationChannel.CALL,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        remote_address="",
        remote_raw="",
        phonegate_event_id_start=1,
        started_at=datetime.now(UTC),
    )
    db.add(call)
    await db.flush()
    db.add(
        CommunicationTurn(
            session_id=call.id,
            phonegate_transcript_id=7,
            seq=1,
            speaker=TurnSpeaker.EMPLOYER,
            text="a",
            occurred_at=datetime.now(UTC),
        )
    )
    await db.flush()
    db.add(
        CommunicationTurn(
            session_id=call.id,
            phonegate_transcript_id=7,
            seq=2,
            speaker=TurnSpeaker.EMPLOYER,
            text="b",
            occurred_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()
