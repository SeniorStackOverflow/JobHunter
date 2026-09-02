from __future__ import annotations

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
