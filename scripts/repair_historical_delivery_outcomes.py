from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.audit import record_audit_event
from app.contacts import (
    ContactDiscoveryService,
    propagate_email_delivery_failure,
    select_best_email_contact,
    validate_public_email,
)
from app.database.session import async_session_factory
from app.models.entities import Alert, Application, EmailDelivery, EmployerContact, SourceJob
from app.models.enums import (
    ApplicationStatus,
    ContactDeliveryState,
    ContactType,
    DeliveryStatus,
)

_TERMINAL = {
    DeliveryStatus.BOUNCED_PERMANENT,
    DeliveryStatus.RECIPIENT_REJECTED,
    DeliveryStatus.DOMAIN_REJECTED,
    DeliveryStatus.POLICY_REJECTED,
    DeliveryStatus.SPAM_REJECTED,
    DeliveryStatus.PERMANENT_FAILURE,
}


def _cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--before must include a timezone")
    return parsed.astimezone(UTC)


async def repair(*, before: datetime, apply: bool) -> dict[str, int]:
    stats = {
        "eligible": 0,
        "already_repaired": 0,
        "applications_failed": 0,
        "contacts_backfilled": 0,
        "audits_recorded": 0,
        "historical_alerts_acknowledged": 0,
    }
    async with async_session_factory() as session:
        deliveries = list(
            (
                await session.scalars(
                    select(EmailDelivery)
                    .where(
                        EmailDelivery.status.in_(_TERMINAL),
                        EmailDelivery.bounced_at.is_not(None),
                        EmailDelivery.bounced_at < before,
                    )
                    .order_by(EmailDelivery.bounced_at, EmailDelivery.id)
                )
            ).all()
        )
        stats["eligible"] = len(deliveries)

        for delivery in deliveries:
            metadata = dict(delivery.sanitized_provider_response or {})
            application = await session.get(Application, delivery.application_id)
            if application is None:
                continue

            if apply:
                historical_alert = await session.scalar(
                    select(Alert).where(
                        Alert.code == f"email_permanent_delivery_failure:{delivery.id}",
                        Alert.acknowledged.is_(False),
                    )
                )
                if historical_alert is not None and bool(
                    (historical_alert.safe_diagnostics or {}).get("historical_repair")
                ):
                    historical_alert.acknowledged = True
                    stats["historical_alerts_acknowledged"] += 1

                if delivery.bounced_at is not None and delivery.failure_class in {
                    "recipient_not_found",
                    "recipient_rejected",
                }:
                    await propagate_email_delivery_failure(
                        session,
                        email=delivery.final_recipient or delivery.recipient,
                        employer_id=application.employer_id,
                        state=(
                            ContactDeliveryState.INVALID
                            if delivery.failure_class == "recipient_not_found"
                            else ContactDeliveryState.REJECTED
                        ),
                        occurred_at=delivery.bounced_at,
                        smtp_status=delivery.smtp_status,
                        failure_reason=delivery.failure_class,
                    )

            if metadata.get("historical_repair_no_retry") is True:
                stats["already_repaired"] += 1
                continue

            source_job = await session.get(SourceJob, application.source_job_id)

            alternate_recipient: str | None = None
            if apply and source_job is not None:
                before_count = len(
                    (
                        await session.scalars(
                            select(EmployerContact.id).where(
                                EmployerContact.source_job_id == source_job.id,
                                EmployerContact.contact_type == ContactType.EMAIL,
                            )
                        )
                    ).all()
                )
                await ContactDiscoveryService().discover_email_contacts(session, source_job)
                after_contacts = list(
                    (
                        await session.scalars(
                            select(EmployerContact).where(
                                EmployerContact.source_job_id == source_job.id,
                                EmployerContact.contact_type == ContactType.EMAIL,
                            )
                        )
                    ).all()
                )
                stats["contacts_backfilled"] += max(0, len(after_contacts) - before_count)
                current_public = {
                    normalized
                    for value in [source_job.public_email, *(source_job.public_emails or [])]
                    if value and (normalized := validate_public_email(value))
                }
                alternatives = [
                    contact
                    for contact in after_contacts
                    if contact.id != application.recipient_contact_id
                    and contact.value in current_public
                ]
                alternate = select_best_email_contact(alternatives)
                alternate_recipient = alternate.value if alternate is not None else None

            if not apply:
                continue

            if application.status is ApplicationStatus.SENT:
                application.status = ApplicationStatus.FAILED
                stats["applications_failed"] += 1

            delivery.next_retry_at = None
            metadata["historical_repair_no_retry"] = True
            metadata["recipient_fallback_pending"] = False
            delivery.sanitized_provider_response = metadata

            contact = await session.get(EmployerContact, application.recipient_contact_id)
            if contact is not None:
                if delivery.failure_class == "recipient_not_found":
                    contact.delivery_state = ContactDeliveryState.INVALID
                elif contact.delivery_state not in {
                    ContactDeliveryState.INVALID,
                    ContactDeliveryState.SUPPRESSED,
                }:
                    contact.delivery_state = ContactDeliveryState.REJECTED

            await record_audit_event(
                session,
                actor="delivery_repair",
                action="email.historical_delivery_repaired",
                entity_type="email_delivery",
                entity_id=str(delivery.id),
                correlation_id=str(application.id),
                decision=delivery.status.value,
                details={
                    "historical_repair_no_retry": True,
                    "alternate_recipient": alternate_recipient,
                },
            )
            stats["audits_recorded"] += 1

        if apply:
            await session.commit()
        else:
            await session.rollback()
    return stats


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True, type=_cutoff)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(await repair(before=args.before, apply=args.apply))


if __name__ == "__main__":
    asyncio.run(_main())
