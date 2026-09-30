"""Daily hard-maximum ledger of provider submissions.

The daily *maximum* limits how many messages were handed to the provider on one
Europe/Chisinau calendar day. It is deliberately separate from the daily
*minimum*, which counts confirmed successful sends (see
``app.applications.daily_target``):

* an attempt is reserved (``in_flight``) under the serialized quota lock before
  the provider call, so concurrent workers cannot exceed the maximum;
* a provider acceptance keeps consuming the day it happened even if a later DSN
  reports a bounce, because the message was already transmitted;
* an uncertain outcome (``delivery_unknown``) keeps its reservation;
* a provider refusal before acceptance (temporary/permanent error, reauth) did
  not transmit a message and releases the reservation;
* a retry on another local day is a new attempt counted on that day.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.models.entities import Application, EmailDelivery, EmailSendAttempt
from app.time_utils import LOCAL_TZ, local_day_bounds

OUTCOME_IN_FLIGHT = "in_flight"
OUTCOME_PROVIDER_ACCEPTED = "provider_accepted"
OUTCOME_DELIVERY_UNKNOWN = "delivery_unknown"
OUTCOME_NOT_TRANSMITTED = "not_transmitted"

COUNTED_OUTCOMES = frozenset(
    {OUTCOME_IN_FLIGHT, OUTCOME_PROVIDER_ACCEPTED, OUTCOME_DELIVERY_UNKNOWN}
)


def local_day_key(value: datetime | None = None) -> str:
    """Return the Europe/Chisinau calendar day of ``value`` (default: now)."""

    if value is None:
        start_local, _start, _end = local_day_bounds()
        return start_local.date().isoformat()
    aware = value if value.tzinfo is not None else value.replace(tzinfo=LOCAL_TZ)
    return aware.astimezone(LOCAL_TZ).date().isoformat()


async def transmissions_on_day(
    session: AsyncSession,
    *,
    profile_id: UUID,
    day: str | None = None,
) -> int:
    """Count provider submissions that consume the profile's hard maximum."""

    return int(
        await session.scalar(
            select(func.count(EmailSendAttempt.id)).where(
                EmailSendAttempt.profile_id == profile_id,
                EmailSendAttempt.local_day == (day or local_day_key()),
                EmailSendAttempt.outcome.in_(COUNTED_OUTCOMES),
            )
        )
        or 0
    )


async def transmissions_by_profile(
    session: AsyncSession,
    *,
    day: str | None = None,
) -> dict[UUID, int]:
    rows = (
        await session.execute(
            select(EmailSendAttempt.profile_id, func.count(EmailSendAttempt.id))
            .where(
                EmailSendAttempt.local_day == (day or local_day_key()),
                EmailSendAttempt.outcome.in_(COUNTED_OUTCOMES),
            )
            .group_by(EmailSendAttempt.profile_id)
        )
    ).all()
    return {profile_id: int(count) for profile_id, count in rows}


async def reserve_send_attempt(
    session: AsyncSession,
    *,
    delivery: EmailDelivery,
    application: Application,
    now: datetime | None = None,
) -> EmailSendAttempt:
    """Reserve hard-maximum capacity for the provider call about to be made.

    Must be called inside the transaction that holds the daily quota lock and
    committed before the provider call.
    """

    started_at = now or utcnow()
    # A new delivery receives its primary key only when it is flushed.
    await session.flush()
    attempt = EmailSendAttempt(
        delivery_id=delivery.id,
        application_id=application.id,
        profile_id=application.profile_id,
        attempt_no=delivery.attempt_count,
        local_day=local_day_key(started_at),
        outcome=OUTCOME_IN_FLIGHT,
        started_at=started_at,
    )
    session.add(attempt)
    return attempt


def finish_send_attempt(
    attempt: EmailSendAttempt,
    outcome: str,
    *,
    now: datetime | None = None,
) -> None:
    if outcome not in COUNTED_OUTCOMES | {OUTCOME_NOT_TRANSMITTED}:
        raise ValueError(f"unknown send attempt outcome: {outcome}")
    attempt.outcome = outcome
    attempt.finished_at = now or utcnow()


async def mark_in_flight_attempts_unknown(
    session: AsyncSession,
    *,
    delivery_id: UUID,
) -> None:
    """Record that an abandoned in-flight attempt has an unknown provider outcome."""

    for attempt in (
        await session.scalars(
            select(EmailSendAttempt).where(
                EmailSendAttempt.delivery_id == delivery_id,
                EmailSendAttempt.outcome == OUTCOME_IN_FLIGHT,
            )
        )
    ).all():
        finish_send_attempt(attempt, OUTCOME_DELIVERY_UNKNOWN)


__all__ = [
    "COUNTED_OUTCOMES",
    "OUTCOME_DELIVERY_UNKNOWN",
    "OUTCOME_IN_FLIGHT",
    "OUTCOME_NOT_TRANSMITTED",
    "OUTCOME_PROVIDER_ACCEPTED",
    "finish_send_attempt",
    "local_day_key",
    "mark_in_flight_attempts_unknown",
    "reserve_send_attempt",
    "transmissions_by_profile",
    "transmissions_on_day",
]
