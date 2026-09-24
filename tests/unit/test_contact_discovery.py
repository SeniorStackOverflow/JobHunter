from __future__ import annotations

from sqlalchemy import select

from app.contacts import ContactDiscoveryService
from app.models.entities import CanonicalJob, EmployerContact, JobSource, SourceJob
from app.models.enums import (
    ContactDeliveryState,
    ContactType,
    JobStatus,
    SourceHealth,
    VerificationStatus,
)


async def test_discovery_persists_all_emails_and_prefers_hr_alias(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        source = JobSource(
            name="Rabota fixture",
            base_url="https://www.rabota.md",
            adapter_type="fixture_source",
            configuration={},
            health_status=SourceHealth.HEALTHY,
        )
        canonical = CanonicalJob(
            normalized_company="centrul national de tenis ntc",
            normalized_title="cleaner",
            normalized_location="chisinau",
            canonical_fingerprint="a" * 64,
            status=JobStatus.ACTIVE,
        )
        session.add_all([source, canonical])
        await session.flush()
        job = SourceJob(
            source_id=source.id,
            canonical_job_id=canonical.id,
            external_job_id="143984",
            canonical_url="https://www.rabota.md/job/143984",
            localized_urls={},
            employer_url="https://www.rabota.md/companies/ntc",
            title="Cleaner",
            company="NTC",
            categories_seen=["operating"],
            category="operating",
            description="Fixture",
            cities=["Chisinau"],
            location="Chisinau",
            public_email="r@satulgerman.md",
            public_emails=["r@satulgerman.md", "hr@satulgerman.md"],
            content_hash="b" * 64,
            matching_content_hash="b" * 64,
            source_fingerprint="c" * 64,
            status=JobStatus.ACTIVE,
            raw_metadata={},
        )
        session.add(job)
        await session.flush()

        selected = await ContactDiscoveryService().discover_from_source_job(session, job)
        contacts = list(
            (
                await session.scalars(
                    select(EmployerContact)
                    .where(
                        EmployerContact.source_job_id == job.id,
                        EmployerContact.contact_type == ContactType.EMAIL,
                    )
                    .order_by(EmployerContact.value)
                )
            ).all()
        )

        assert [contact.value for contact in contacts] == [
            "hr@satulgerman.md",
            "r@satulgerman.md",
        ]
        assert selected is not None
        assert selected.value == "hr@satulgerman.md"
        assert all(
            contact.verification_status is VerificationStatus.SOURCE_VERIFIED
            for contact in contacts
        )
        assert all(contact.official_domain == "satulgerman.md" for contact in contacts)


async def test_rejected_first_email_stays_rejected_and_alternate_is_selected(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        source = JobSource(
            name="Rabota fixture",
            base_url="https://www.rabota.md",
            adapter_type="fixture_source",
            configuration={},
            health_status=SourceHealth.HEALTHY,
        )
        canonical = CanonicalJob(
            normalized_company="example",
            normalized_title="role",
            normalized_location="chisinau",
            canonical_fingerprint="d" * 64,
            status=JobStatus.ACTIVE,
        )
        session.add_all([source, canonical])
        await session.flush()
        job = SourceJob(
            source_id=source.id,
            canonical_job_id=canonical.id,
            external_job_id="multi-email",
            canonical_url="https://www.rabota.md/job/multi-email",
            localized_urls={},
            title="Role",
            company="Example",
            categories_seen=["office"],
            category="office",
            description="Fixture",
            cities=["Chisinau"],
            public_email="r@example.md",
            public_emails=["r@example.md", "hr@example.md"],
            content_hash="e" * 64,
            matching_content_hash="e" * 64,
            source_fingerprint="f" * 64,
            status=JobStatus.ACTIVE,
            raw_metadata={},
        )
        session.add(job)
        await session.flush()
        rejected = EmployerContact(
            canonical_job_id=canonical.id,
            source_job_id=job.id,
            value="r@example.md",
            contact_type=ContactType.EMAIL,
            discovery_source="job_detail_explicit_email",
            official_domain="example.md",
            verification_status=VerificationStatus.VERIFIED,
            confidence=0.9,
            evidence_url=job.canonical_url,
            delivery_state=ContactDeliveryState.INVALID,
            failure_count=1,
        )
        session.add(rejected)
        await session.flush()

        selected = await ContactDiscoveryService().discover_from_source_job(session, job)
        await session.flush()

        assert selected is not None
        assert selected.value == "hr@example.md"
        assert rejected.delivery_state is ContactDeliveryState.INVALID
        assert rejected.failure_count == 1
        assert rejected.verification_status is VerificationStatus.SOURCE_VERIFIED
