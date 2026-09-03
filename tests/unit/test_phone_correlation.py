from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import (
    Application,
    CanonicalJob,
    EmployerContact,
    JobSource,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    ContactType,
    JobStatus,
    VerificationStatus,
)
from app.phone.correlation import CallerCorrelation


@pytest_asyncio.fixture
async def db(sqlite_session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    async with sqlite_session_factory() as session:
        session.add(UserProfile(name="default", is_default=True))
        await session.commit()
        yield session


async def _job_with_phone(db: AsyncSession, phone: str) -> tuple[SourceJob, CanonicalJob]:
    src = JobSource(name="s", base_url="https://x", adapter_type="fixture_source")
    db.add(src)
    await db.flush()
    canonical = CanonicalJob(
        normalized_company="ACME",
        normalized_title="Loader",
        canonical_fingerprint=uuid4().hex,
        status=JobStatus.ACTIVE,
    )
    db.add(canonical)
    await db.flush()
    job = SourceJob(
        source_id=src.id,
        canonical_job_id=canonical.id,
        external_job_id=uuid4().hex,
        canonical_url="https://x/1",
        title="Loader",
        content_hash="h",
        matching_content_hash="m",
        source_fingerprint="f",
        public_phone=phone,
        status=JobStatus.ACTIVE,
    )
    db.add(job)
    await db.flush()
    return job, canonical


async def _canonical(db: AsyncSession, *, source: JobSource) -> tuple[SourceJob, CanonicalJob]:
    canonical = CanonicalJob(
        normalized_company="ACME",
        normalized_title="Loader",
        canonical_fingerprint=uuid4().hex,
        status=JobStatus.ACTIVE,
    )
    db.add(canonical)
    await db.flush()
    job = SourceJob(
        source_id=source.id,
        canonical_job_id=canonical.id,
        external_job_id=uuid4().hex,
        canonical_url=f"https://x/{uuid4().hex}",
        title="Loader",
        content_hash="h",
        matching_content_hash="m",
        source_fingerprint="f",
        status=JobStatus.ACTIVE,
    )
    db.add(job)
    await db.flush()
    return job, canonical


async def _phone_contact(
    db: AsyncSession, *, job: SourceJob, canonical: CanonicalJob, phone: str
) -> EmployerContact:
    contact = EmployerContact(
        canonical_job_id=canonical.id,
        source_job_id=job.id,
        value=phone,
        contact_type=ContactType.PHONE,
        discovery_source="test",
        verification_status=VerificationStatus.UNVERIFIED,
        confidence=0.6,
        evidence_url="https://x/1",
    )
    db.add(contact)
    await db.flush()
    return contact


async def test_no_default_profile_returns_none(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        result = await CallerCorrelation().resolve(session, "+37360111222")
        assert result is None


async def test_unknown_number_falls_back_to_default_profile(
    db: AsyncSession,
) -> None:
    result = await CallerCorrelation().resolve(db, "+37360999888")
    assert result is not None
    assert result.application_id is None
    assert result.contact_id is None


async def test_matches_existing_phone_contact_and_application(
    db: AsyncSession,
) -> None:
    job, canonical = await _job_with_phone(db, "+37360111222")
    profile = await db.scalar(select(UserProfile))
    contact = EmployerContact(
        canonical_job_id=canonical.id,
        source_job_id=job.id,
        value="+37360111222",
        contact_type=ContactType.PHONE,
        discovery_source="test",
        verification_status=VerificationStatus.UNVERIFIED,
        confidence=0.6,
        evidence_url="https://x/1",
    )
    db.add(contact)
    await db.flush()

    app = Application(
        profile_id=profile.id,
        canonical_job_id=canonical.id,
        source_job_id=job.id,
        resume_id=uuid4(),
        recipient_contact_id=contact.id,
        subject="s",
        body="b",
        language="ru",
        status=ApplicationStatus.PENDING_REVIEW,
        idempotency_key=uuid4().hex,
    )
    db.add(app)
    await db.flush()

    result = await CallerCorrelation().resolve(db, "+373 60 111 222")
    assert result is not None
    assert result.canonical_job_id == canonical.id
    assert result.application_id is not None


async def test_creates_phone_contact_from_source_job(
    db: AsyncSession,
) -> None:
    _, _ = await _job_with_phone(db, "+37360111222")
    result = await CallerCorrelation().resolve(db, "+37360111222")
    assert result is not None
    assert result.contact_id is not None
    contacts = (await db.execute(EmployerContact.__table__.select())).scalars().all()
    assert len(contacts) == 1

    # second resolve of the same number must NOT create a duplicate contact
    result2 = await CallerCorrelation().resolve(db, "+37360111222")
    assert result2 is not None
    assert result2.contact_id == result.contact_id
    contacts_after = (await db.execute(EmployerContact.__table__.select())).scalars().all()
    assert len(contacts_after) == 1


async def test_matches_public_phones_array(
    db: AsyncSession,
) -> None:
    src = JobSource(name="s", base_url="https://x", adapter_type="fixture_source")
    db.add(src)
    await db.flush()
    canonical = CanonicalJob(
        normalized_company="ACME",
        normalized_title="Loader",
        canonical_fingerprint=uuid4().hex,
        status=JobStatus.ACTIVE,
    )
    db.add(canonical)
    await db.flush()
    job = SourceJob(
        source_id=src.id,
        canonical_job_id=canonical.id,
        external_job_id=uuid4().hex,
        canonical_url="https://x/1",
        title="Loader",
        content_hash="h",
        matching_content_hash="m",
        source_fingerprint="f",
        public_phone=None,
        public_phones=["+37360111222", "+37360000000"],
        status=JobStatus.ACTIVE,
    )
    db.add(job)
    await db.flush()

    # Resolve with a number from the public_phones array (formatted differently)
    result = await CallerCorrelation().resolve(db, "+373 60 111 222")
    assert result is not None
    assert result.canonical_job_id == canonical.id
    assert result.contact_id is not None
    contacts = (await db.execute(EmployerContact.__table__.select())).scalars().all()
    assert len(contacts) == 1

    # second resolve of the same number must NOT create a duplicate contact
    result2 = await CallerCorrelation().resolve(db, "+373 60 111 222")
    assert result2 is not None
    assert result2.contact_id == result.contact_id
    contacts_after = (await db.execute(EmployerContact.__table__.select())).scalars().all()
    assert len(contacts_after) == 1


async def test_agency_line_across_two_jobs_is_ambiguous(db: AsyncSession) -> None:
    """F3 / HIGH: one number registered as a phone contact for two distinct
    canonical jobs cannot be attributed to a single job/application."""
    src = JobSource(name="s", base_url="https://x", adapter_type="fixture_source")
    db.add(src)
    await db.flush()
    job_a, canon_a = await _canonical(db, source=src)
    job_b, canon_b = await _canonical(db, source=src)
    await _phone_contact(db, job=job_a, canonical=canon_a, phone="+37360111222")
    await _phone_contact(db, job=job_b, canonical=canon_b, phone="+37360111222")

    result = await CallerCorrelation().resolve(db, "+373 60 111 222")
    assert result is not None
    assert result.ambiguous is True
    profile = await db.scalar(select(UserProfile))
    assert result.profile_id == profile.id
    assert result.application_id is None
    assert result.canonical_job_id is None


async def test_two_applications_on_one_job_is_ambiguous(db: AsyncSession) -> None:
    """F3 / HIGH: the job is known but two profiles applied to it — the call
    cannot be pinned to one profile/application."""
    src = JobSource(name="s", base_url="https://x", adapter_type="fixture_source")
    db.add(src)
    await db.flush()
    job, canon = await _canonical(db, source=src)
    contact = await _phone_contact(db, job=job, canonical=canon, phone="+37360111222")

    default_profile = await db.scalar(select(UserProfile))
    other_profile = UserProfile(name="other", is_default=False)
    db.add(other_profile)
    await db.flush()
    for profile in (default_profile, other_profile):
        db.add(
            Application(
                profile_id=profile.id,
                canonical_job_id=canon.id,
                source_job_id=job.id,
                resume_id=uuid4(),
                recipient_contact_id=contact.id,
                subject="s",
                body="b",
                language="ru",
                status=ApplicationStatus.PENDING_REVIEW,
                idempotency_key=uuid4().hex,
            )
        )
    await db.flush()

    result = await CallerCorrelation().resolve(db, "+37360111222")
    assert result is not None
    assert result.ambiguous is True
    assert result.profile_id == default_profile.id
    assert result.application_id is None
    assert result.canonical_job_id == canon.id
    assert result.contact_id == contact.id


async def test_public_phones_match_is_not_capped_at_200(db: AsyncSession) -> None:
    """F3 / MEDIUM: the public_phones[] scan used to LIMIT 200 most-recent jobs,
    silently missing an older vacancy whose number matches."""
    src = JobSource(name="s", base_url="https://x", adapter_type="fixture_source")
    db.add(src)
    await db.flush()

    now = datetime.now(UTC)
    # 220 more-recent active jobs with unrelated phones
    for _ in range(220):
        await _canonical(db, source=src)
    # rewrite last_seen_at so they all sit ahead of the target
    jobs = (await db.scalars(select(SourceJob))).all()
    for j in jobs:
        j.last_seen_at = now
    await db.flush()

    target_job, target_canon = await _canonical(db, source=src)
    target_job.public_phones = ["+37360111222"]
    target_job.last_seen_at = now - timedelta(days=400)
    await db.flush()

    result = await CallerCorrelation().resolve(db, "+373 60 111 222")
    assert result is not None
    assert result.ambiguous is False
    assert result.canonical_job_id == target_canon.id
    assert result.contact_id is not None


async def test_two_jobs_with_same_scalar_public_phone_is_ambiguous(db: AsyncSession) -> None:
    """F3 review round 3 / HIGH: the scalar public_phone branch must also be
    ambiguity-safe — two vacancies sharing that column is not attributable."""
    src = JobSource(name="s", base_url="https://x", adapter_type="fixture_source")
    db.add(src)
    await db.flush()
    job_a, _ = await _canonical(db, source=src)
    job_b, _ = await _canonical(db, source=src)
    job_a.public_phone = "+37360111222"
    job_b.public_phone = "+37360111222"
    await db.flush()

    result = await CallerCorrelation().resolve(db, "+373 60 111 222")
    assert result is not None
    assert result.ambiguous is True
    assert result.canonical_job_id is None
    assert result.application_id is None


async def test_scalar_and_array_phone_on_different_jobs_is_ambiguous(db: AsyncSession) -> None:
    """One job carries the number in the scalar column, another in the array —
    the two are gathered together so the ambiguity is caught."""
    src = JobSource(name="s", base_url="https://x", adapter_type="fixture_source")
    db.add(src)
    await db.flush()
    job_a, _ = await _canonical(db, source=src)
    job_b, _ = await _canonical(db, source=src)
    job_a.public_phone = "+37360111222"
    job_b.public_phones = ["+37360111222"]
    await db.flush()

    result = await CallerCorrelation().resolve(db, "+37360111222")
    assert result is not None
    assert result.ambiguous is True


async def test_public_phones_match_ignores_job_status(db: AsyncSession) -> None:
    """F3 review / HIGH: a call-back about a role that has since closed must still
    correlate — the array scan no longer filters on JobStatus.ACTIVE."""
    src = JobSource(name="s", base_url="https://x", adapter_type="fixture_source")
    db.add(src)
    await db.flush()
    job, canon = await _canonical(db, source=src)
    job.public_phones = ["+37360111222"]
    job.status = JobStatus.CLOSED
    canon.status = JobStatus.CLOSED
    await db.flush()

    result = await CallerCorrelation().resolve(db, "+373 60 111 222")
    assert result is not None
    assert result.ambiguous is False
    assert result.canonical_job_id == canon.id
    assert result.contact_id is not None


async def test_public_phones_match_on_two_jobs_is_ambiguous(db: AsyncSession) -> None:
    src = JobSource(name="s", base_url="https://x", adapter_type="fixture_source")
    db.add(src)
    await db.flush()
    job_a, _ = await _canonical(db, source=src)
    job_b, _ = await _canonical(db, source=src)
    job_a.public_phones = ["+37360111222"]
    job_b.public_phones = ["+37360111222"]
    await db.flush()

    result = await CallerCorrelation().resolve(db, "+37360111222")
    assert result is not None
    assert result.ambiguous is True
    assert result.canonical_job_id is None
