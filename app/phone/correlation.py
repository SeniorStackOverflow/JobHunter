from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Application, EmployerContact, SourceJob, UserProfile
from app.models.enums import ContactType, VerificationStatus
from app.phone.numbers import normalize_e164


@dataclass(frozen=True, slots=True)
class CorrelationResult:
    profile_id: UUID
    application_id: UUID | None
    canonical_job_id: UUID | None
    source_job_id: UUID | None
    contact_id: UUID | None


def _domain(value: str | None) -> str | None:
    if not value:
        return None
    return (urlsplit(value).hostname or "").lower() or None


class CallerCorrelation:
    def __init__(self, *, region: str = "MD") -> None:
        self._region = region

    async def resolve(self, session: AsyncSession, remote_raw: str) -> CorrelationResult | None:
        default_profile_id = await session.scalar(
            select(UserProfile.id).where(UserProfile.is_default.is_(True)).limit(1)
        )
        if default_profile_id is None:
            return None

        e164 = normalize_e164(remote_raw, region=self._region)
        if e164 is None:
            return CorrelationResult(default_profile_id, None, None, None, None)

        contact = await session.scalar(
            select(EmployerContact)
            .where(
                EmployerContact.contact_type == ContactType.PHONE,
                EmployerContact.value == e164,
            )
            .order_by(EmployerContact.confidence.desc(), EmployerContact.created_at.desc())
            .limit(1)
        )
        job: SourceJob | None = None
        if contact is None:
            job = await session.scalar(
                select(SourceJob)
                .where(SourceJob.public_phone == e164)
                .order_by(SourceJob.last_seen_at.desc())
                .limit(1)
            )
            if job is not None and job.canonical_job_id is not None:
                contact = EmployerContact(
                    canonical_job_id=job.canonical_job_id,
                    source_job_id=job.id,
                    value=e164,
                    contact_type=ContactType.PHONE,
                    discovery_source="inbound_call_match_public_phone",
                    official_domain=_domain(job.employer_url or job.canonical_url),
                    verification_status=VerificationStatus.UNVERIFIED,
                    confidence=0.6,
                    evidence_url=job.canonical_url,
                )
                session.add(contact)
                await session.flush()

        if contact is None:
            return CorrelationResult(default_profile_id, None, None, None, None)

        application = await session.scalar(
            select(Application)
            .where(Application.canonical_job_id == contact.canonical_job_id)
            .order_by(Application.created_at.desc())
            .limit(1)
        )
        profile_id = application.profile_id if application is not None else default_profile_id
        return CorrelationResult(
            profile_id=profile_id,
            application_id=application.id if application is not None else None,
            canonical_job_id=contact.canonical_job_id,
            source_job_id=contact.source_job_id,
            contact_id=contact.id,
        )
