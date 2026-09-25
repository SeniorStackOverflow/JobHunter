from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import EmployerContact, SourceJob
from app.models.enums import ContactDeliveryState, ContactType, VerificationStatus

_TERMINAL_DELIVERY_STATES = {
    ContactDeliveryState.INVALID,
    ContactDeliveryState.REJECTED,
    ContactDeliveryState.SUPPRESSED,
}

_PREFERRED_LOCAL_PARTS = {
    "hr": 50,
    "jobs": 48,
    "job": 46,
    "careers": 46,
    "career": 44,
    "recruitment": 44,
    "recruiting": 44,
    "recrutare": 44,
    "recrutari": 44,
    "hiring": 42,
    "vacancies": 40,
    "vacancy": 38,
}

_GENERIC_LOCAL_PARTS = {
    "info": -5,
    "office": -5,
    "contact": -5,
    "hello": -5,
    "mail": -8,
}

_BLOCKED_LOCAL_PARTS = {
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
}


def _domain(value: str | None) -> str | None:
    if not value:
        return None
    return (urlsplit(value).hostname or "").lower() or None


def validate_public_email(value: str) -> str | None:
    """Normalize syntax only; this does not prove that the mailbox exists."""
    try:
        result = validate_email(value, check_deliverability=False, test_environment=True)
    except EmailNotValidError:
        return None
    return result.normalized.lower()


def contact_is_source_verified(contact: EmployerContact) -> bool:
    """Return whether a contact is explicitly sourced and eligible for policy checks.

    VERIFIED is retained for legacy rows. New vacancy-owned contacts use
    SOURCE_VERIFIED so syntax/source provenance is not confused with mailbox
    deliverability.
    """
    return contact.verification_status in {
        VerificationStatus.SOURCE_VERIFIED,
        VerificationStatus.VERIFIED,
    }


def _recipient_score(contact: EmployerContact) -> float:
    if contact.contact_type is not ContactType.EMAIL:
        return -100_000
    if not contact_is_source_verified(contact):
        return -100_000
    if contact.delivery_state in _TERMINAL_DELIVERY_STATES:
        return -100_000

    local_part = contact.value.rsplit("@", maxsplit=1)[0].casefold()
    if local_part in _BLOCKED_LOCAL_PARTS:
        return -100_000

    score = contact.confidence * 10
    if contact.delivery_state is ContactDeliveryState.HEALTHY:
        score += 100
    elif contact.delivery_state is ContactDeliveryState.TRANSIENT_FAILURE:
        score -= 25

    score += _PREFERRED_LOCAL_PARTS.get(local_part, 0)
    score += _GENERIC_LOCAL_PARTS.get(local_part, 0)
    if len(local_part) == 1:
        score -= 30
    elif len(local_part) == 2 and local_part not in _PREFERRED_LOCAL_PARTS:
        score -= 10
    return score


def select_best_email_contact(contacts: list[EmployerContact]) -> EmployerContact | None:
    usable = [contact for contact in contacts if _recipient_score(contact) > -100_000]
    if not usable:
        return None
    # The email address is a deterministic tie-breaker so HTML/source ordering
    # cannot silently change the selected recipient.
    return max(usable, key=lambda contact: (_recipient_score(contact), contact.value))


async def _delivery_history_source(
    session: AsyncSession,
    *,
    email: str,
    employer_id: UUID | None,
) -> EmployerContact | None:
    invalid = await session.scalar(
        select(EmployerContact)
        .where(
            EmployerContact.contact_type == ContactType.EMAIL,
            EmployerContact.value == email,
            EmployerContact.delivery_state == ContactDeliveryState.INVALID,
            EmployerContact.last_failure_reason == "recipient_not_found",
        )
        .order_by(
            EmployerContact.last_delivery_failure_at.desc(),
            EmployerContact.created_at.desc(),
        )
        .limit(1)
    )
    if invalid is not None:
        return invalid
    if employer_id is None:
        return None
    rejected: EmployerContact | None = await session.scalar(
        select(EmployerContact)
        .where(
            EmployerContact.contact_type == ContactType.EMAIL,
            EmployerContact.value == email,
            EmployerContact.employer_id == employer_id,
            EmployerContact.delivery_state == ContactDeliveryState.REJECTED,
        )
        .order_by(
            EmployerContact.last_delivery_failure_at.desc(),
            EmployerContact.created_at.desc(),
        )
        .limit(1)
    )
    return rejected


def _inherit_delivery_history(target: EmployerContact, source: EmployerContact) -> None:
    if target.delivery_state in {ContactDeliveryState.INVALID, ContactDeliveryState.SUPPRESSED}:
        return
    target.delivery_state = source.delivery_state
    target.last_delivery_failure_at = source.last_delivery_failure_at
    target.last_smtp_status = source.last_smtp_status
    target.last_failure_reason = source.last_failure_reason


async def propagate_email_delivery_failure(
    session: AsyncSession,
    *,
    email: str,
    employer_id: UUID | None,
    state: ContactDeliveryState,
    occurred_at: datetime,
    smtp_status: str | None,
    failure_reason: str,
) -> None:
    normalized = validate_public_email(email)
    if normalized is None or state not in {
        ContactDeliveryState.INVALID,
        ContactDeliveryState.REJECTED,
    }:
        return
    query = select(EmployerContact).where(
        EmployerContact.contact_type == ContactType.EMAIL,
        EmployerContact.value == normalized,
    )
    if state is ContactDeliveryState.REJECTED:
        if employer_id is None:
            return
        query = query.where(EmployerContact.employer_id == employer_id)
    contacts = list((await session.scalars(query)).all())
    for contact in contacts:
        if contact.delivery_state is ContactDeliveryState.SUPPRESSED:
            continue
        if state is ContactDeliveryState.INVALID:
            contact.delivery_state = ContactDeliveryState.INVALID
        elif contact.delivery_state is not ContactDeliveryState.INVALID:
            contact.delivery_state = ContactDeliveryState.REJECTED
        if contact.last_delivery_failure_at is None or (
            (
                contact.last_delivery_failure_at.replace(tzinfo=UTC)
                if contact.last_delivery_failure_at.tzinfo is None
                else contact.last_delivery_failure_at.astimezone(UTC)
            )
            <= occurred_at
        ):
            contact.last_delivery_failure_at = occurred_at
            contact.last_smtp_status = smtp_status
            contact.last_failure_reason = failure_reason


class ContactDiscoveryService:
    async def discover_email_contacts(
        self, session: AsyncSession, job: SourceJob
    ) -> list[EmployerContact]:
        """Persist every explicit public email and preserve delivery history."""
        if job.canonical_job_id is None:
            raise ValueError("job must be assigned to a canonical job first")

        contacts: list[EmployerContact] = []
        email_values = [job.public_email, *(job.public_emails or [])]
        for raw_email in dict.fromkeys(value for value in email_values if value):
            email = validate_public_email(raw_email)
            if not email:
                continue
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
                if (
                    existing.employer_id is not None
                    and job.employer_id is not None
                    and existing.employer_id != job.employer_id
                ):
                    continue
                if existing.employer_id is None:
                    existing.employer_id = job.employer_id
                if existing.discovery_source == "job_detail_explicit_email":
                    existing.verification_status = VerificationStatus.SOURCE_VERIFIED
                history = await _delivery_history_source(
                    session,
                    email=email,
                    employer_id=job.employer_id,
                )
                if history is not None and history.id != existing.id:
                    _inherit_delivery_history(existing, history)
                contacts.append(existing)
                continue

            email_domain = email.rsplit("@", maxsplit=1)[1]
            contact = EmployerContact(
                canonical_job_id=job.canonical_job_id,
                employer_id=job.employer_id,
                source_job_id=job.id,
                value=email,
                contact_type=ContactType.EMAIL,
                discovery_source="job_detail_explicit_email",
                # The Rabota company page is not the employer's own domain.
                official_domain=email_domain,
                verification_status=VerificationStatus.SOURCE_VERIFIED,
                confidence=0.9,
                evidence_url=job.canonical_url,
            )
            history = await _delivery_history_source(
                session,
                email=email,
                employer_id=job.employer_id,
            )
            if history is not None:
                _inherit_delivery_history(contact, history)
            session.add(contact)
            await session.flush()
            contacts.append(contact)
        return contacts

    async def discover_from_source_job(
        self, session: AsyncSession, job: SourceJob
    ) -> EmployerContact | None:
        email_contacts = await self.discover_email_contacts(session, job)
        selected_email = select_best_email_contact(email_contacts)
        if selected_email is not None:
            return selected_email

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
                employer_id=job.employer_id,
                source_job_id=job.id,
                value=job.application_url,
                contact_type=ContactType.APPLICATION_URL,
                discovery_source="job_detail_application_url",
                official_domain=_domain(job.application_url),
                verification_status=VerificationStatus.SOURCE_VERIFIED,
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
                employer_id=job.employer_id,
                source_job_id=job.id,
                value=job.canonical_url,
                contact_type=ContactType.INTERNAL_JOB_BOARD,
                discovery_source="public_job_board_application_control",
                official_domain=_domain(job.canonical_url),
                verification_status=VerificationStatus.SOURCE_VERIFIED,
                confidence=0.95,
                evidence_url=job.canonical_url,
            )
            session.add(contact)
            await session.flush()
            return contact
        return None


__all__ = [
    "ContactDiscoveryService",
    "contact_is_source_verified",
    "propagate_email_delivery_failure",
    "select_best_email_contact",
    "validate_public_email",
]
