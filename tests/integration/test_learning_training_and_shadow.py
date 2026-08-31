from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.learning.service import ReviewLearningService
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
