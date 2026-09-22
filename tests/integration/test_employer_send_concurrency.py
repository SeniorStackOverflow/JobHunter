from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.employers import EmployerRelationshipService
from app.models.entities import (
    Application,
    CanonicalEmployer,
    CanonicalJob,
    EmployerContact,
    EmployerInteractionEvent,
    JobSource,
    MatchEvaluation,
    Resume,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    ContactType,
    EmployerInteractionChannel,
    EmployerInteractionType,
    JobStatus,
    MatchDecision,
    VerificationStatus,
)

SERVICES_ENABLED = os.environ.get("RUN_SERVICE_INTEGRATION_TESTS") == "1"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not SERVICES_ENABLED,
        reason="set RUN_SERVICE_INTEGRATION_TESTS=1 with PostgreSQL available",
    ),
]


async def _seed_graph(session: AsyncSession) -> tuple[UUID, UUID]:
    suffix = uuid4().hex
    profile = UserProfile(name=f"Concurrency {suffix}", is_default=False)
    employer = CanonicalEmployer(normalized_name=f"concurrency-{suffix}")
    source = JobSource(
        name=f"Concurrency {suffix}",
        base_url=f"https://{suffix}.example.test",
        adapter_type="fixture_source",
        configuration={},
    )
    session.add_all([profile, employer, source])
    await session.flush()
    resume = Resume(
        profile_id=profile.id,
        name="Fixture",
        category="general",
        storage_key=f"{suffix}.pdf",
        original_filename="fixture.pdf",
        mime_type="application/pdf",
        sha256="a" * 64,
        active=True,
        verified=True,
    )
    session.add(resume)
    await session.flush()
    applications: list[Application] = []
    for index, score in enumerate((95, 85), start=1):
        canonical = CanonicalJob(
            employer_id=employer.id,
            normalized_company=employer.normalized_name,
            normalized_title=f"role {index}",
            normalized_location="",
            canonical_fingerprint=f"{index}{suffix}".ljust(64, "0")[:64],
            status=JobStatus.ACTIVE,
        )
        session.add(canonical)
        await session.flush()
        job = SourceJob(
            source_id=source.id,
            canonical_job_id=canonical.id,
            employer_id=employer.id,
            external_job_id=f"{suffix}-{index}",
            canonical_url=f"https://{suffix}.example.test/{index}",
            localized_urls={},
            title=f"Role {index}",
            company="Concurrency Employer",
            categories_seen=[],
            cities=[],
            public_email=f"jobs{index}@{suffix}.example.test",
            public_emails=[f"jobs{index}@{suffix}.example.test"],
            content_hash=str(index) * 64,
            matching_content_hash=str(index) * 64,
            source_fingerprint=f"{9 - index}" * 64,
            status=JobStatus.ACTIVE,
            raw_metadata={},
        )
        session.add(job)
        await session.flush()
        evaluation = MatchEvaluation(
            profile_id=profile.id,
            canonical_job_id=canonical.id,
            source_job_id=job.id,
            resume_fit=score,
            preference_fit=score,
            overall_fit=score,
            requirements_met=[],
            missing_requirements=[],
            risks=[],
            scam_indicators=[],
            explanation="fixture",
            decision=MatchDecision.AUTO_APPLY,
            model="fixture",
            prompt_rules_version="fixture",
        )
        contact = EmployerContact(
            canonical_job_id=canonical.id,
            employer_id=employer.id,
            source_job_id=job.id,
            value=f"jobs{index}@{suffix}.example.test",
            contact_type=ContactType.EMAIL,
            discovery_source="fixture",
            verification_status=VerificationStatus.VERIFIED,
            confidence=1,
            evidence_url=job.canonical_url,
        )
        session.add_all([evaluation, contact])
        await session.flush()
        application = Application(
            profile_id=profile.id,
            canonical_job_id=canonical.id,
            employer_id=employer.id,
            source_job_id=job.id,
            match_evaluation_id=evaluation.id,
            resume_id=resume.id,
            recipient_contact_id=contact.id,
            subject="Fixture",
            body="Fixture",
            language="en",
            status=ApplicationStatus.PREPARED,
            idempotency_key=f"{index}{suffix}".ljust(64, "0")[:64],
        )
        session.add(application)
        applications.append(application)
    await session.commit()
    return applications[0].id, applications[1].id


@pytest.mark.asyncio
async def test_two_workers_competing_for_one_employer_slot_only_authorize_one() -> None:
    database_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = EmployerRelationshipService()
    async with factory() as session:
        first_id, second_id = await _seed_graph(session)
    first_locked = asyncio.Event()
    release_first = asyncio.Event()

    async def contender(application_id: UUID, *, pause: bool) -> bool:
        async with factory() as session:
            application = await session.get(Application, application_id)
            assert application is not None and application.match_evaluation_id is not None
            evaluation = await session.get(MatchEvaluation, application.match_evaluation_id)
            job = await session.get(SourceJob, application.source_job_id)
            assert (
                evaluation is not None and job is not None and application.employer_id is not None
            )
            await service.lock_employer(session, application.employer_id)
            if pause:
                first_locked.set()
                await release_first.wait()
            outcome = await service.policy_outcome(
                session,
                application=application,
                evaluation=evaluation,
                job=job,
            )
            if outcome.slot_available:
                application.status = ApplicationStatus.SENT
            await session.commit()
            return outcome.slot_available

    first_task = asyncio.create_task(contender(first_id, pause=True))
    await first_locked.wait()
    second_task = asyncio.create_task(contender(second_id, pause=False))
    await asyncio.sleep(0.1)
    release_first.set()
    results = await asyncio.gather(first_task, second_task)

    assert list(results) == [True, False]
    async with factory() as session:
        sent = list(
            (
                await session.scalars(
                    select(Application).where(
                        Application.id.in_([first_id, second_id]),
                        Application.status == ApplicationStatus.SENT,
                    )
                )
            ).all()
        )
        assert len(sent) == 1

    async def ingest_same_event() -> UUID:
        async with factory() as session:
            application = await session.get(Application, first_id)
            assert application is not None and application.employer_id is not None
            event, _created = await service.record_event(
                session,
                profile_id=application.profile_id,
                employer_id=application.employer_id,
                event_type=EmployerInteractionType.EMPLOYER_REPLIED,
                channel=EmployerInteractionChannel.EMAIL,
                idempotency_key=f"concurrent-provider-event:{first_id}",
                application_id=application.id,
            )
            await session.commit()
            return event.id

    event_ids = await asyncio.gather(ingest_same_event(), ingest_same_event())
    assert event_ids[0] == event_ids[1]
    async with factory() as session:
        event_count = await session.scalar(
            select(func.count(EmployerInteractionEvent.id)).where(
                EmployerInteractionEvent.idempotency_key == f"concurrent-provider-event:{first_id}"
            )
        )
        assert event_count == 1
    await engine.dispose()
