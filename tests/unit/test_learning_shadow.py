from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.learning.shadow import agreement_of, shadow_scorecard
from app.models.entities import LearningModelVersion, LearningShadowOutcome, UserProfile
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


async def test_scorecard_dedupes_to_the_newest_model_generation(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="p", is_default=True)
        session.add(profile)
        await session.flush()

        def version(trained_at: datetime, *, cv_ran: bool) -> LearningModelVersion:
            return LearningModelVersion(
                profile_id=profile.id,
                segment_key="global",
                feature_spec_version="features-v3",
                algorithm="l2_logistic_isotonic",
                payload={},
                n_labels=50,
                cv_auc=0.7,
                cv_logloss=0.5,
                cv_ece=0.05,
                cv_ran=cv_ran,
                trained_at=trained_at,
            )

        old = version(datetime(2026, 8, 1, tzinfo=UTC), cv_ran=True)
        new = version(datetime(2026, 8, 20, tzinfo=UTC), cv_ran=False)
        session.add_all([old, new])
        await session.flush()

        application_id = uuid4()

        def row(model: LearningModelVersion, decide: ShadowDecision) -> LearningShadowOutcome:
            return LearningShadowOutcome(
                profile_id=profile.id,
                application_id=application_id,
                model_version_id=model.id,
                segment_key="global",
                p_approve=0.5,
                ci_low=0.4,
                ci_high=0.6,
                support_ok=True,
                would_decide=decide,
                human_decision=ReviewOutcome.APPROVED,
                agreed=decide is ShadowDecision.APPROVE,
            )

        # one long-queued application, scored once per nightly model generation
        session.add_all(
            [
                row(old, ShadowDecision.REJECT),  # old generation disagreed
                row(new, ShadowDecision.APPROVE),  # new generation agreed
            ]
        )
        await session.flush()

        card = await shadow_scorecard(session, profile.id)

        assert card["cases_total"] == 1  # one application, not two rows
        assert card["would_approve"] == 1  # the newest generation's call
        assert card["would_reject"] == 0
        assert card["agreement_overall"] == pytest.approx(1.0)
        assert card["model"]["cv_ran"] is False  # headline labelled with newest version
