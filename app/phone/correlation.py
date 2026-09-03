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
    ambiguous: bool = False


def _domain(value: str | None) -> str | None:
    if not value:
        return None
    return (urlsplit(value).hostname or "").lower() or None


def _contact_from_job(
    *,
    canonical_job_id: UUID,
    source_job_id: UUID,
    e164: str,
    official_domain: str | None,
    evidence_url: str,
) -> EmployerContact:
    return EmployerContact(
        canonical_job_id=canonical_job_id,
        source_job_id=source_job_id,
        value=e164,
        contact_type=ContactType.PHONE,
        discovery_source="inbound_call_match_public_phone",
        official_domain=official_domain,
        verification_status=VerificationStatus.UNVERIFIED,
        confidence=0.6,
        evidence_url=evidence_url,
    )


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

        # 1. Existing phone contacts carrying this number.
        contacts = list(
            await session.scalars(
                select(EmployerContact)
                .where(
                    EmployerContact.contact_type == ContactType.PHONE,
                    EmployerContact.value == e164,
                )
                .order_by(EmployerContact.confidence.desc(), EmployerContact.created_at.desc())
            )
        )
        if len({c.canonical_job_id for c in contacts}) > 1:
            # One number, several vacancies (agency line) — the call cannot be attributed.
            return CorrelationResult(default_profile_id, None, None, None, None, ambiguous=True)
        contact = contacts[0] if contacts else None

        if contact is None:
            # 2. Match SourceJob by phone — the scalar public_phone column AND the
            # public_phones[] array, gathered together so ambiguity across the two
            # is caught (one number on two different vacancies -> not attributable).
            # No status filter (steps 1 & 4 don't filter either; a call-back about
            # a role that has since closed is the common case). No row cap.
            # TODO: a normalized, indexed employer-phone table would remove this
            # full scan (deferred past Phase 1).
            rows = (
                await session.execute(
                    select(
                        SourceJob.id,
                        SourceJob.canonical_job_id,
                        SourceJob.public_phone,
                        SourceJob.public_phones,
                        SourceJob.employer_url,
                        SourceJob.canonical_url,
                    )
                    .where(SourceJob.canonical_job_id.is_not(None))
                    .order_by(SourceJob.last_seen_at.desc())
                )
            ).all()
            matches = [
                r
                for r in rows
                if r.public_phone == e164 or (r.public_phones and e164 in r.public_phones)
            ]
            if len({r.canonical_job_id for r in matches}) > 1:
                return CorrelationResult(default_profile_id, None, None, None, None, ambiguous=True)
            if matches:
                r = matches[0]  # rows are already newest-first
                contact = _contact_from_job(
                    canonical_job_id=r.canonical_job_id,
                    source_job_id=r.id,
                    e164=e164,
                    official_domain=_domain(r.employer_url or r.canonical_url),
                    evidence_url=r.canonical_url,
                )
                session.add(contact)
                await session.flush()

        if contact is None:
            return CorrelationResult(default_profile_id, None, None, None, None)

        # 4. Applications on the (single) canonical job.
        applications = list(
            await session.scalars(
                select(Application)
                .where(Application.canonical_job_id == contact.canonical_job_id)
                .order_by(Application.created_at.desc())
            )
        )
        if len(applications) > 1:
            # Several profiles applied to this job — job known, target profile unknown.
            return CorrelationResult(
                profile_id=default_profile_id,
                application_id=None,
                canonical_job_id=contact.canonical_job_id,
                source_job_id=contact.source_job_id,
                contact_id=contact.id,
                ambiguous=True,
            )
        application = applications[0] if applications else None
        profile_id = application.profile_id if application is not None else default_profile_id
        return CorrelationResult(
            profile_id=profile_id,
            application_id=application.id if application is not None else None,
            canonical_job_id=contact.canonical_job_id,
            source_job_id=contact.source_job_id,
            contact_id=contact.id,
        )
