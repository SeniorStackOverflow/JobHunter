from __future__ import annotations

from urllib.parse import urlsplit

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import EmployerContact, SourceJob
from app.models.enums import ContactType, VerificationStatus


def _domain(value: str | None) -> str | None:
    if not value:
        return None
    return (urlsplit(value).hostname or "").lower() or None


def validate_public_email(value: str) -> str | None:
    try:
        result = validate_email(value, check_deliverability=False, test_environment=True)
    except EmailNotValidError:
        return None
    return result.normalized.lower()


class ContactDiscoveryService:
    async def discover_from_source_job(
        self, session: AsyncSession, job: SourceJob
    ) -> EmployerContact | None:
        if job.canonical_job_id is None:
            raise ValueError("job must be assigned to a canonical job first")
        if job.public_email:
            email = validate_public_email(job.public_email)
            if email:
                existing: EmployerContact | None = await session.scalar(
                    select(EmployerContact)
                    .where(
                        EmployerContact.source_job_id == job.id,
                        EmployerContact.contact_type == ContactType.EMAIL,
                        EmployerContact.value == email,
                    )
                    .order_by(EmployerContact.confidence.desc())
                    .limit(1)
                )
                if existing is not None:
                    return existing
                email_domain = email.rsplit("@", maxsplit=1)[1]
                employer_domain = _domain(job.employer_url)
                contact = EmployerContact(
                    canonical_job_id=job.canonical_job_id,
                    source_job_id=job.id,
                    value=email,
                    contact_type=ContactType.EMAIL,
                    discovery_source="job_detail_explicit_email",
                    official_domain=employer_domain or email_domain,
                    verification_status=VerificationStatus.VERIFIED,
                    confidence=0.98 if employer_domain == email_domain else 0.9,
                    evidence_url=job.canonical_url,
                )
                session.add(contact)
                await session.flush()
                return contact
        if job.application_url:
            application_contact = await session.scalar(
                select(EmployerContact)
                .where(
                    EmployerContact.source_job_id == job.id,
                    EmployerContact.contact_type == ContactType.APPLICATION_URL,
                    EmployerContact.value == job.application_url,
                )
                .order_by(EmployerContact.confidence.desc())
                .limit(1)
            )
            if application_contact is not None:
                return application_contact
            contact = EmployerContact(
                canonical_job_id=job.canonical_job_id,
                source_job_id=job.id,
                value=job.application_url,
                contact_type=ContactType.APPLICATION_URL,
                discovery_source="job_detail_application_url",
                official_domain=_domain(job.application_url),
                verification_status=VerificationStatus.VERIFIED,
                confidence=0.8,
                evidence_url=job.canonical_url,
            )
            session.add(contact)
            await session.flush()
            return contact
        if job.raw_metadata.get("internal_application_available") is True:
            existing = await session.scalar(
                select(EmployerContact)
                .where(
                    EmployerContact.source_job_id == job.id,
                    EmployerContact.contact_type == ContactType.INTERNAL_JOB_BOARD,
                    EmployerContact.value == job.canonical_url,
                )
                .limit(1)
            )
            if existing is not None:
                return existing
            contact = EmployerContact(
                canonical_job_id=job.canonical_job_id,
                source_job_id=job.id,
                value=job.canonical_url,
                contact_type=ContactType.INTERNAL_JOB_BOARD,
                discovery_source="public_job_board_application_control",
                official_domain=_domain(job.canonical_url),
                verification_status=VerificationStatus.VERIFIED,
                confidence=0.95,
                evidence_url=job.canonical_url,
            )
            session.add(contact)
            await session.flush()
            return contact
        return None
