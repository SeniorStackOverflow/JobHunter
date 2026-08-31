from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.learning.shadow import agreement_of, shadow_scorecard
from app.models.entities import LearningShadowOutcome, UserProfile
from app.models.enums import ReviewOutcome, ShadowDecision


def test_agreement_matrix() -> None:
    assert agreement_of(ShadowDecision.APPROVE, ReviewOutcome.APPROVED) is True
    assert agreement_of(ShadowDecision.APPROVE, ReviewOutcome.REJECTED) is False
    assert agreement_of(ShadowDecision.REJECT, ReviewOutcome.REJECTED) is True
    assert agreement_of(ShadowDecision.REJECT, ReviewOutcome.APPROVED) is False
    assert agreement_of(ShadowDecision.ABSTAIN, ReviewOutcome.APPROVED) is None


async def test_scorecard_counts_and_agreement(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="p", is_default=True)
        session.add(profile)
        await session.flush()

        def row(decide, human, agreed, support=True):
            return LearningShadowOutcome(
                profile_id=profile.id,
                application_id=uuid4(),
                model_version_id=None,
                segment_key="global",
                p_approve=0.5,
                ci_low=0.4,
                ci_high=0.6,
                support_ok=support,
                would_decide=decide,
                human_decision=human,
                agreed=agreed,
            )

        session.add_all(
            [
                row(ShadowDecision.APPROVE, ReviewOutcome.APPROVED, True),
                row(ShadowDecision.APPROVE, ReviewOutcome.REJECTED, False),
                row(ShadowDecision.REJECT, ReviewOutcome.REJECTED, True),
                row(ShadowDecision.ABSTAIN, None, None, support=False),
            ]
        )
        await session.flush()

        card = await shadow_scorecard(session, profile.id)

        assert card["cases_total"] == 4
        assert card["resolved"] == 3
        assert card["would_approve"] == 2
        assert card["agreement_overall"] == pytest.approx(2 / 3)
        assert card["would_approve_agreement"] == pytest.approx(0.5)
        assert card["support_ok_rate"] == pytest.approx(0.75)
        assert card["model"] is None
