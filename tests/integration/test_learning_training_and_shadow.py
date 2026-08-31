from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.learning.training import latest_model, train_profile
from app.models.entities import (
    LearningModelVersion,
    LearningShadowOutcome,
    ReviewFeedbackEvent,
    UserProfile,
)
from app.models.enums import ReviewOutcome, ReviewReason

pytestmark = pytest.mark.asyncio


async def test_new_learning_tables_are_created(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        assert (await session.scalars(select(LearningModelVersion))).all() == []
        assert (await session.scalars(select(LearningShadowOutcome))).all() == []


def _feedback(profile_id, outcome, category, day) -> ReviewFeedbackEvent:
    return ReviewFeedbackEvent(
        profile_id=profile_id,
        application_id=uuid4(),
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        outcome=outcome,
        reason_code=None if outcome == ReviewOutcome.APPROVED else ReviewReason.ROLE,
        actor="test",
        learning_eligible=True,
        feature_schema_version="review-v2",
        feature_snapshot={
            "features": {
                "category": [category],
                "title": ["picker" if category == "warehouses" else "agent"],
            },
            "learning_dimensions": ["category", "title"],
            "numeric": {"overall_fit": 70.0 if category == "warehouses" else 30.0},
        },
        created_at=datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=day),
    )


async def test_train_profile_needs_enough_balanced_labels(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="p", is_default=True)
        session.add(profile)
        await session.flush()
        session.add_all(
            [_feedback(profile.id, ReviewOutcome.APPROVED, "warehouses", d) for d in range(5)]
        )
        await session.flush()

        assert await train_profile(session, profile.id) is None


async def test_train_profile_writes_a_usable_model(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="p", is_default=True)
        session.add(profile)
        await session.flush()
        events = []
        for d in range(30):
            events.append(_feedback(profile.id, ReviewOutcome.APPROVED, "warehouses", d))
        for d in range(30, 55):
            events.append(_feedback(profile.id, ReviewOutcome.REJECTED, "sales", d))
        session.add_all(events)
        await session.flush()

        version = await train_profile(session, profile.id)
        assert version is not None
        assert version.n_labels == 55
        assert version.segment_key == "global"

        model = await latest_model(session, profile.id)
        assert model is not None
        assert model.feature_spec_version == "features-v3"
