from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.applications import ApplicationService
from app.applications.service import (
    ApplicationPreparationError,
    prepare_pending_applications,
)
from app.contacts import ContactDiscoveryService
from app.email.providers import FakeGmailProvider
from app.email.service import EmailSendBlocked, EmailService
from app.matching.bindings import (
    confirmed_fact_hashes,
    preference_fingerprint,
    profile_fingerprint,
)
from app.matching.service import _priority_rematch_source_ids
from app.models.entities import (
    Application,
    AuditEvent,
    CanonicalJob,
    EmailDelivery,
    JobPreference,
    JobSnapshot,
    JobSource,
    MatchEvaluation,
    Resume,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    DeliveryStatus,
    JobStatus,
    MatchDecision,
    PolicyDecision,
    SourceHealth,
)
from app.settings import Settings


def _settings(storage: Path) -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        resume_storage_path=storage,
        email_provider="fake",
        real_email_delivery_enabled=False,
    )


async def _duplicate_graph(session, storage: Path) -> dict[str, object]:
    now = datetime.now(UTC)
    source_a = JobSource(
        name="Board A",
        base_url="https://a.example.com",
        adapter_type="fixture_source",
        configuration={},
        health_status=SourceHealth.HEALTHY,
    )
    source_b = JobSource(
        name="Board B",
        base_url="https://b.example.com",
        adapter_type="fixture_source",
        configuration={},
        health_status=SourceHealth.HEALTHY,
    )
    profile = UserProfile(
        id=uuid4(),
        name="Candidate",
        languages=[{"code": "en", "confirmed": True}],
        confirmed_facts=[],
    )
    preference = JobPreference(
        profile_id=profile.id,
        allowed_categories=["technology"],
        auto_send_categories=["technology"],
        maximum_daily_applications=3,
        minimum_auto_send_score=80,
        auto_send_enabled=True,
        global_pause=False,
    )
    resume_data = b"%PDF-1.7\nevaluation binding test"
    await asyncio.to_thread(storage.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread((storage / "resume.pdf").write_bytes, resume_data)
    resume = Resume(
        profile_id=profile.id,
        name="Technical CV",
        category="technology",
        storage_key="resume.pdf",
        original_filename="resume.pdf",
        mime_type="application/pdf",
        sha256=hashlib.sha256(resume_data).hexdigest(),
        active=True,
        verified=True,
        is_default=True,
    )
    canonical = CanonicalJob(
        normalized_company="shared company",
        normalized_title="backend engineer",
        normalized_location="chisinau",
        canonical_fingerprint="c" * 64,
        status=JobStatus.ACTIVE,
    )
    session.add_all([source_a, source_b, profile, preference, resume, canonical])
    await session.flush()

    common = {
        "canonical_job_id": canonical.id,
        "localized_urls": {},
        "title": "Backend Engineer",
        "company": "Shared Company",
        "categories_seen": ["technology"],
        "category": "technology",
        "description": "Build Python APIs.",
        "cities": ["Chisinau"],
        "location": "Chisinau",
        "page_locale": "en",
        "status": JobStatus.ACTIVE,
        "raw_metadata": {},
    }
    # Publication A was seen most recently. Publication B has the newest match
    # evaluation, which is the precise arrangement that previously mixed rows.
    job_a = SourceJob(
        source_id=source_a.id,
        external_job_id="a-1",
        canonical_url="https://a.example.com/jobs/a-1",
        public_email="jobs-a@example.com",
        employer_url="https://example.com",
        content_hash="a" * 64,
        source_fingerprint="1" * 64,
        first_seen_at=now - timedelta(days=2),
        last_seen_at=now,
        **common,
    )
    job_b = SourceJob(
        source_id=source_b.id,
        external_job_id="b-1",
        canonical_url="https://b.example.com/jobs/b-1",
        public_email="jobs-b@example.com",
        employer_url="https://example.com",
        content_hash="b" * 64,
        source_fingerprint="2" * 64,
        first_seen_at=now - timedelta(days=2),
        last_seen_at=now - timedelta(hours=1),
        **common,
    )
    session.add_all([job_a, job_b])
    await session.flush()
    canonical.primary_source_job_id = job_a.id

    evaluation_a = MatchEvaluation(
        profile_id=profile.id,
        canonical_job_id=canonical.id,
        source_job_id=job_a.id,
        resume_fit=85,
        preference_fit=90,
        overall_fit=86,
        requirements_met=[],
        missing_requirements=[],
        risks=[],
        scam_indicators=[],
        explanation="A was evaluated first",
        decision=MatchDecision.AUTO_APPLY,
        model="mock",
        prompt_rules_version="test",
        source_content_hash=job_a.content_hash,
        resume_id=resume.id,
        resume_sha256=resume.sha256,
        profile_fingerprint=profile_fingerprint(profile),
        preference_fingerprint=preference_fingerprint(preference),
        confirmed_fact_hashes=confirmed_fact_hashes(profile),
        created_at=now - timedelta(minutes=2),
    )
    evaluation_b = MatchEvaluation(
        profile_id=profile.id,
        canonical_job_id=canonical.id,
        source_job_id=job_b.id,
        resume_fit=95,
        preference_fit=96,
        overall_fit=95,
        requirements_met=[],
        missing_requirements=[],
        risks=[],
        scam_indicators=[],
        explanation="B is the newest evaluation",
        decision=MatchDecision.AUTO_APPLY,
        model="mock",
        prompt_rules_version="test",
        source_content_hash=job_b.content_hash,
        resume_id=resume.id,
        resume_sha256=resume.sha256,
        profile_fingerprint=profile_fingerprint(profile),
        preference_fingerprint=preference_fingerprint(preference),
        confirmed_fact_hashes=confirmed_fact_hashes(profile),
        created_at=now - timedelta(minutes=1),
    )
    session.add_all([evaluation_a, evaluation_b])
    await session.flush()
    return {
        "profile": profile,
        "canonical": canonical,
        "job_a": job_a,
        "job_b": job_b,
        "evaluation_a": evaluation_a,
        "evaluation_b": evaluation_b,
        "resume": resume,
    }


async def test_prepare_binds_one_evaluation_source_pair_when_duplicate_recency_differs(
    sqlite_session_factory,
    tmp_path: Path,
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _duplicate_graph(session, tmp_path)
        application = await ApplicationService(_settings(tmp_path)).prepare(
            session,
            graph["canonical"].id,
        )

        assert application.source_job_id == graph["job_b"].id
        assert application.match_evaluation_id == graph["evaluation_b"].id
        contact = await ContactDiscoveryService().discover_from_source_job(
            session,
            graph["job_b"],
        )
        assert contact is not None
        assert application.recipient_contact_id == contact.id
        assert contact.value == "jobs-b@example.com"


async def test_stale_pending_application_is_selected_for_priority_rematch(
    sqlite_session_factory,
    tmp_path: Path,
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _duplicate_graph(session, tmp_path)
        application = await ApplicationService(_settings(tmp_path)).prepare(
            session,
            graph["canonical"].id,
        )
        application.status = ApplicationStatus.PENDING_REVIEW
        application.policy_decision = PolicyDecision.PENDING_REVIEW
        application.policy_result = {
            "decision": "pending_review",
            "rules_passed": [],
            "rules_failed": ["match_evaluation_current"],
            "policy_version": "test",
            "safe_stop_reason": "match_evaluation_stale",
            "requires_rematch": True,
        }
        await session.flush()

        priority = await _priority_rematch_source_ids(session, graph["profile"].id)

        assert graph["job_b"].id in priority


async def test_manual_approval_rejects_markerless_stale_evaluation(
    sqlite_session_factory,
    tmp_path: Path,
) -> None:
    current_settings = _settings(tmp_path)
    async with sqlite_session_factory() as session:
        graph = await _duplicate_graph(session, tmp_path)
        application = await ApplicationService(current_settings).prepare(
            session,
            graph["canonical"].id,
        )
        assert application.source_job_id == graph["job_b"].id
        application.status = ApplicationStatus.PENDING_REVIEW
        application_id = application.id
        graph["job_b"].content_hash = "7" * 64
        await session.commit()

        with pytest.raises(ApplicationPreparationError, match="match evaluation is stale"):
            await ApplicationService(current_settings).approve(session, application_id)
        await session.rollback()

    async with sqlite_session_factory() as session:
        stored = await session.get(Application, application_id)
        assert stored is not None
        assert stored.status == ApplicationStatus.PENDING_REVIEW


async def test_sender_cannot_use_newest_duplicate_evaluation_for_stale_publication(
    sqlite_session_factory,
    tmp_path: Path,
) -> None:
    fake = FakeGmailProvider()
    async with sqlite_session_factory() as session:
        graph = await _duplicate_graph(session, tmp_path)
        job_a = graph["job_a"]
        evaluation_a = graph["evaluation_a"]
        contact_a = await ContactDiscoveryService().discover_from_source_job(session, job_a)
        assert contact_a is not None
        old_hash = job_a.content_hash
        job_a.description = "The publication changed after matching."
        job_a.content_hash = "d" * 64
        session.add(
            JobSnapshot(
                source_job_id=job_a.id,
                changed_fields=["description"],
                description=job_a.description,
                salary={},
                requirements=None,
                contacts={"email": job_a.public_email},
                content_hash=job_a.content_hash,
                timestamp=evaluation_a.created_at + timedelta(seconds=1),
            )
        )
        application = Application(
            profile_id=graph["profile"].id,
            canonical_job_id=graph["canonical"].id,
            source_job_id=job_a.id,
            match_evaluation_id=evaluation_a.id,
            resume_id=graph["resume"].id,
            recipient_contact_id=contact_a.id,
            subject="Application for Backend Engineer",
            body="Hello Shared Company team, I am applying for Backend Engineer.",
            language="en",
            status=ApplicationStatus.AUTO_APPROVED,
            used_confirmed_facts=[],
            content_validated=True,
            idempotency_key="f" * 64,
        )
        session.add(application)
        await session.commit()
        application_id = application.id
        assert evaluation_a.source_content_hash == old_hash

    service = EmailService(_settings(tmp_path), sqlite_session_factory, fake)
    with pytest.raises(EmailSendBlocked, match="evaluation is stale"):
        await service.send_application(application_id)

    assert fake.outbox == []
    async with sqlite_session_factory() as session:
        persisted = await session.get(Application, application_id)
        assert persisted is not None
        assert persisted.status == ApplicationStatus.PENDING_REVIEW
        assert persisted.policy_decision == PolicyDecision.PENDING_REVIEW
        assert persisted.policy_result["safe_stop_reason"] == "match_evaluation_stale"
        assert persisted.policy_result["requires_rematch"] is True
        assert "match_evaluation_current" in persisted.policy_result["rules_failed"]
        assert persisted.match_evaluation_id == graph["evaluation_a"].id


@pytest.mark.e2e
async def test_stale_auto_approved_application_is_rematched_prepared_and_sent(
    sqlite_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.applications import service as application_service
    from app.email import service as email_service
    from app.matching import service as matching_service

    current_settings = _settings(tmp_path)
    monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
    monkeypatch.setattr(matching_service, "get_settings", lambda: current_settings)
    monkeypatch.setattr(application_service, "get_settings", lambda: current_settings)
    monkeypatch.setattr(email_service, "get_settings", lambda: current_settings)

    async with sqlite_session_factory() as session:
        graph = await _duplicate_graph(session, tmp_path)
        application = await ApplicationService(current_settings).prepare(
            session,
            graph["canonical"].id,
        )
        assert application.source_job_id == graph["job_b"].id
        assert application.status == ApplicationStatus.AUTO_APPROVED
        graph["job_b"].description = "Updated active publication requiring a fresh match."
        graph["job_b"].content_hash = "9" * 64
        application_id = application.id
        original_evaluation_id = application.match_evaluation_id
        await session.commit()

    assert await email_service.send_auto_approved_applications() == 0
    async with sqlite_session_factory() as session:
        pending = await session.get(Application, application_id)
        assert pending is not None
        assert pending.status == ApplicationStatus.PENDING_REVIEW
        assert pending.policy_result["safe_stop_reason"] == "match_evaluation_stale"

    assert await matching_service.process_unprocessed_jobs() == 2
    assert await application_service.prepare_pending_applications() == 1

    async with sqlite_session_factory() as session:
        refreshed = await session.get(Application, application_id)
        assert refreshed is not None
        assert refreshed.match_evaluation_id != original_evaluation_id
        assert refreshed.status == ApplicationStatus.AUTO_APPROVED
        assert "safe_stop_reason" not in refreshed.policy_result

    assert await email_service.send_auto_approved_applications() == 1
    async with sqlite_session_factory() as session:
        sent = await session.get(Application, application_id)
        delivery = await session.scalar(
            select(EmailDelivery).where(EmailDelivery.application_id == application_id)
        )
        assert sent is not None
        assert sent.status == ApplicationStatus.SENT
        assert delivery is not None
        assert delivery.status == DeliveryStatus.SENT


@pytest.mark.asyncio
async def test_periodic_prepare_does_not_overwrite_manual_approval(
    sqlite_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.database.session as database_session

    monkeypatch.setattr(database_session, "async_session_factory", sqlite_session_factory)
    async with sqlite_session_factory() as session:
        graph = await _duplicate_graph(session, tmp_path)
        application = await ApplicationService(_settings(tmp_path)).prepare(
            session, graph["canonical"].id
        )
        application.status = ApplicationStatus.APPROVED
        approved_evaluation_id = application.match_evaluation_id
        application_id = application.id
        await session.commit()

    prepared = await prepare_pending_applications()

    assert prepared == 0
    async with sqlite_session_factory() as session:
        persisted = await session.get(Application, application_id)
        assert persisted is not None
        assert persisted.status == ApplicationStatus.APPROVED
        assert persisted.match_evaluation_id == approved_evaluation_id


@pytest.mark.asyncio
async def test_periodic_prepare_blocks_unsent_application_when_canonical_vacancy_closes(
    sqlite_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.database.session as database_session

    monkeypatch.setattr(database_session, "async_session_factory", sqlite_session_factory)
    async with sqlite_session_factory() as session:
        graph = await _duplicate_graph(session, tmp_path)
        application = await ApplicationService(_settings(tmp_path)).prepare(
            session, graph["canonical"].id
        )
        application.status = ApplicationStatus.APPROVED
        graph["job_a"].status = JobStatus.CLOSED
        graph["job_b"].status = JobStatus.CLOSED
        graph["canonical"].status = JobStatus.CLOSED
        application_id = application.id
        await session.commit()

    prepared = await prepare_pending_applications()

    assert prepared == 0
    async with sqlite_session_factory() as session:
        persisted = await session.get(Application, application_id)
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "application.blocked_closed_vacancy",
                AuditEvent.entity_id == str(application_id),
            )
        )
        assert persisted is not None
        assert persisted.status == ApplicationStatus.BLOCKED
        assert persisted.policy_decision == PolicyDecision.BLOCKED
        assert "vacancy_active" in persisted.policy_result["rules_failed"]
        assert audit is not None
        assert audit.actor == "application_scheduler"
