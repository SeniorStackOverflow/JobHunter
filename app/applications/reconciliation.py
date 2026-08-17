from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit_event
from app.database.base import utcnow
from app.models.entities import Application, EmailDelivery
from app.models.enums import ApplicationStatus, DeliveryStatus

STALE_DELIVERY_AFTER = timedelta(minutes=15)


class DeliveryReconciliationError(ValueError):
    """A delivery cannot safely be reconciled from its persisted state."""


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def delivery_reconcile_available_at(delivery: EmailDelivery) -> datetime:
    return _as_utc(delivery.updated_at or delivery.created_at) + STALE_DELIVERY_AFTER


def delivery_is_stale(delivery: EmailDelivery, *, now: datetime | None = None) -> bool:
    current = _as_utc(now or utcnow())
    return current >= delivery_reconcile_available_at(delivery)


async def reconcile_stale_delivery_unknown(
    session: AsyncSession,
    application_id: UUID,
    *,
    actor: str,
) -> EmailDelivery:
    """Conservatively resolve an abandoned ``sending`` attempt as unknown.

    This operation never retries or edits recipient/message data.  The age guard is
    intentionally longer than an ordinary provider call so an operator cannot race a
    healthy in-flight request by immediately clicking the reconciliation action.
    """

    application = await session.scalar(
        select(Application).where(Application.id == application_id).with_for_update()
    )
    if application is None:
        raise LookupError(f"application {application_id} does not exist")
    delivery = await session.scalar(
        select(EmailDelivery)
        .where(EmailDelivery.application_id == application_id)
        .with_for_update()
    )
    if delivery is None:
        raise DeliveryReconciliationError("application has no delivery attempt")

    if (
        application.status == ApplicationStatus.DELIVERY_UNKNOWN
        and delivery.status == DeliveryStatus.DELIVERY_UNKNOWN
    ):
        decision = "already_delivery_unknown"
    else:
        if (
            application.status != ApplicationStatus.SENDING
            or delivery.status != DeliveryStatus.SENDING
        ):
            raise DeliveryReconciliationError("only a sending delivery can be reconciled")
        if not delivery_is_stale(delivery):
            raise DeliveryReconciliationError("delivery is not old enough to reconcile safely")
        application.status = ApplicationStatus.DELIVERY_UNKNOWN
        delivery.status = DeliveryStatus.DELIVERY_UNKNOWN
        delivery.error = "operator reconciled a stale sending attempt as delivery_unknown"
        delivery.updated_at = utcnow()
        decision = DeliveryStatus.DELIVERY_UNKNOWN.value

    await record_audit_event(
        session,
        actor=actor,
        action="email.delivery_reconciled",
        entity_type="application",
        entity_id=str(application.id),
        correlation_id=str(application.id),
        decision=decision,
        details={"delivery_id": str(delivery.id), "attempt": delivery.attempt_count},
    )
    await session.flush()
    return delivery


__all__ = [
    "STALE_DELIVERY_AFTER",
    "DeliveryReconciliationError",
    "delivery_is_stale",
    "delivery_reconcile_available_at",
    "reconcile_stale_delivery_unknown",
]
