import pytest
from sqlalchemy import select

from app.models.entities import LearningModelVersion, LearningShadowOutcome

pytestmark = pytest.mark.asyncio


async def test_new_learning_tables_are_created(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        assert (await session.scalars(select(LearningModelVersion))).all() == []
        assert (await session.scalars(select(LearningShadowOutcome))).all() == []
