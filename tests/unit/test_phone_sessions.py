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
