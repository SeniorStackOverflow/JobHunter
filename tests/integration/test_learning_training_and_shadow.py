from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.learning.service import ReviewLearningService
from app.learning.shadow import record_shadow_outcomes, shadow_scorecard
from app.learning.training import latest_model, train_profile
from app.models.entities import (
    Application,
    CanonicalJob,
    EmployerContact,
    JobPreference,
    LearningModelVersion,
    LearningShadowOutcome,
    MatchEvaluation,
    Resume,
    ReviewFeedbackEvent,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    ContactType,
    MatchDecision,
    ReviewOutcome,
    ReviewReason,
    VerificationStatus,
)

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


async def _prepared_application(session) -> Application:
    profile = UserProfile(name="p", is_default=True)
    session.add(profile)
    await session.flush()
    session.add(JobPreference(profile_id=profile.id, minimum_salary=10000))
    canonical = CanonicalJob(
        normalized_company="c", normalized_title="t", canonical_fingerprint=uuid4().hex
    )
    session.add(canonical)
    await session.flush()
    job = SourceJob(
        source_id=uuid4(),
        canonical_job_id=canonical.id,
        external_job_id="x",
        canonical_url="https://e/j",
        title="Picker",
        content_hash="a",
        matching_content_hash="b",
        source_fingerprint="c",
        salary_min=8000,
        raw_metadata={"adapter_type": "rabota_md"},
    )
    resume = Resume(
        profile_id=profile.id,
        name="r",
        category="logistics",
        storage_key=uuid4().hex,
        original_filename="r.pdf",
        mime_type="application/pdf",
        sha256="d" * 64,
        active=True,
        verified=True,
    )
    session.add_all([job, resume])
    await session.flush()
    evaluation = MatchEvaluation(
        profile_id=profile.id,
        canonical_job_id=canonical.id,
        source_job_id=job.id,
        resume_fit=60,
        preference_fit=80,
        overall_fit=72,
        requirements_met=[],
        missing_requirements=[],
        risks=[],
        scam_indicators=[],
        explanation="",
        decision=MatchDecision.PREPARE_FOR_REVIEW,
        model="m",
        prompt_rules_version="v",
        source_content_hash="a",
        source_matching_hash="b",
        resume_id=resume.id,
        resume_sha256="d" * 64,
    )
    contact = EmployerContact(
        canonical_job_id=canonical.id,
        source_job_id=job.id,
        value="hr@e.test",
        contact_type=ContactType.EMAIL,
        discovery_source="page",
        verification_status=VerificationStatus.VERIFIED,
        evidence_url="https://e/j",
    )
    session.add_all([evaluation, contact])
    await session.flush()
    application = Application(
        profile_id=profile.id,
        canonical_job_id=canonical.id,
        source_job_id=job.id,
        match_evaluation_id=evaluation.id,
        resume_id=resume.id,
        recipient_contact_id=contact.id,
        subject="Отклик на вакансию «Picker»",
        body="body",
        language="ru",
        status=ApplicationStatus.PENDING_REVIEW,
        idempotency_key=uuid4().hex,
        content_validated=True,
    )
    session.add(application)
    await session.flush()
    return application


async def test_record_decision_stores_numeric_and_context(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        application = await _prepared_application(session)
        event = await ReviewLearningService().record_decision(
            session, application, outcome=ReviewOutcome.APPROVED, actor="test"
        )
        assert event.feature_schema_version == "review-v2"
        assert event.feature_snapshot["numeric"]["overall_fit"] == 0.72
        assert event.feature_snapshot["numeric"]["salary_gap"] == -0.2
        assert event.feature_snapshot["context"]["source_key"] == "rabota_md"


async def test_shadow_outcome_recorded_for_pending_application(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        application = await _prepared_application(session)
        # enough labels for a model, same profile
        events = []
        for d in range(30):
            events.append(
                _feedback(application.profile_id, ReviewOutcome.APPROVED, "warehouses", d)
            )
        for d in range(30, 55):
            events.append(_feedback(application.profile_id, ReviewOutcome.REJECTED, "sales", d))
        session.add_all(events)
        await session.flush()
        await train_profile(session, application.profile_id)
        await session.flush()

        written = await record_shadow_outcomes(session)
        await session.flush()

        assert written == 1
        outcome = (await session.scalars(select(LearningShadowOutcome))).one()
        assert outcome.application_id == application.id
        assert 0.0 <= outcome.p_approve <= 1.0
        assert outcome.human_decision is None

        # idempotent: no second row, no overwrite
        assert await record_shadow_outcomes(session) == 0


async def test_human_decision_backfills_shadow_agreement(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        application = await _prepared_application(session)
        session.add(
            LearningShadowOutcome(
                profile_id=application.profile_id,
                application_id=application.id,
                model_version_id=None,
                segment_key="global",
                p_approve=0.95,
                ci_low=0.9,
                ci_high=0.98,
                support_ok=True,
                would_decide=__import__(
                    "app.models.enums", fromlist=["ShadowDecision"]
                ).ShadowDecision.APPROVE,
            )
        )
        await session.flush()

        await ReviewLearningService().record_decision(
            session, application, outcome=ReviewOutcome.APPROVED, actor="test"
        )
        await session.flush()

        row = (await session.scalars(select(LearningShadowOutcome))).one()
        assert row.human_decision == ReviewOutcome.APPROVED
        assert row.agreed is True


async def test_full_shadow_loop(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        application = await _prepared_application(session)
        # Alternate outcomes over time so the time-series CV sees both classes in
        # every fold and produces a usable, confident model.
        events = [
            _feedback(application.profile_id, ReviewOutcome.APPROVED, "warehouses", d)
            if d % 2 == 0
            else _feedback(application.profile_id, ReviewOutcome.REJECTED, "sales", d)
            for d in range(60)
        ]
        session.add_all(events)
        await session.flush()

        # train_profile clears the balanced-label gate and writes a model version.
        version = await train_profile(session, application.profile_id)
        assert version is not None
        assert version.n_labels == 60
        await session.flush()

        # The shadow recorder scores the one pending review and writes one row.
        assert await record_shadow_outcomes(session) == 1
        await session.flush()

        # The operator's explicit decision backfills that shadow row.
        await ReviewLearningService().record_decision(
            session, application, outcome=ReviewOutcome.APPROVED, actor="test"
        )
        await session.flush()

        card = await shadow_scorecard(session, application.profile_id)
        assert card["cases_total"] == 1
        assert card["resolved"] == 1
        assert card["model"]["n_labels"] == 60
        assert card["model"]["cv_ran"] is True  # alternating labels -> CV ran


async def test_shadow_recorder_skips_a_model_with_a_stale_feature_layout(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        application = await _prepared_application(session)
        events = [
            _feedback(application.profile_id, ReviewOutcome.APPROVED, "warehouses", d)
            if d % 2 == 0
            else _feedback(application.profile_id, ReviewOutcome.REJECTED, "sales", d)
            for d in range(60)
        ]
        session.add_all(events)
        await session.flush()

        version = await train_profile(session, application.profile_id)
        assert version is not None
        # simulate a code change that reshaped the feature layout: the stored
        # feature_names no longer matches the spec the current code rebuilds.
        payload = dict(version.payload)
        payload["feature_names"] = [*payload["feature_names"], "obs:__unknown_dimension__"]
        version.payload = payload
        await session.flush()

        # the mismatched model is skipped, not misapplied -> no row written
        assert await record_shadow_outcomes(session) == 0
        assert (await session.scalars(select(LearningShadowOutcome))).all() == []


async def test_train_all_profiles_isolates_a_failing_profile(
    sqlite_session_factory, monkeypatch
) -> None:
    import app.learning.training as training_module

    monkeypatch.setattr(training_module, "async_session_factory", sqlite_session_factory)

    async with sqlite_session_factory() as session:
        good = UserProfile(name="good", is_default=True)
        bad = UserProfile(name="bad")
        session.add_all([good, bad])
        await session.flush()
        good_id, bad_id = good.id, bad.id
        events = [
            _feedback(good_id, ReviewOutcome.APPROVED, "warehouses", d) for d in range(30)
        ] + [_feedback(good_id, ReviewOutcome.REJECTED, "sales", d) for d in range(30, 55)]
        session.add_all(events)
        await session.commit()

    real_train_profile = training_module.train_profile

    async def flaky_train_profile(session, profile_id):
        if profile_id == bad_id:
            raise RuntimeError("boom")
        return await real_train_profile(session, profile_id)

    monkeypatch.setattr(training_module, "train_profile", flaky_train_profile)

    written = await training_module.train_all_profiles()

    assert written == 1  # the good profile trained despite the bad one raising
    async with sqlite_session_factory() as session:
        versions = (await session.scalars(select(LearningModelVersion))).all()
        assert [v.profile_id for v in versions] == [good_id]
