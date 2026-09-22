from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    CanonicalEmployer,
    EmployerInteractionEvent,
    EmployerRelationship,
)


def _value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


async def get_relationship(
    session: AsyncSession, *, profile_id: UUID, employer_id: UUID
) -> dict[str, Any]:
    employer = await session.get(CanonicalEmployer, employer_id)
    if employer is None:
        raise LookupError("employer does not exist")
    relationship = await session.scalar(
        select(EmployerRelationship).where(
            EmployerRelationship.profile_id == profile_id,
            EmployerRelationship.employer_id == employer_id,
        )
    )
    return {
        "employer": {
            "id": str(employer.id),
            "normalized_name": employer.normalized_name,
            "primary_domain": employer.primary_domain,
        },
        "profile_id": str(profile_id),
        "relationship": (
            {
                "state": _value(relationship.state),
                "last_interaction_at": relationship.last_interaction_at,
                "last_application_at": relationship.last_application_at,
                "last_interview_at": relationship.last_interview_at,
                "suppression_scope": _value(relationship.suppression_scope),
                "suppression_reason": relationship.suppression_reason,
                "suppressed_until": relationship.suppressed_until,
                "suppressed_by_event_id": relationship.suppressed_by_event_id,
                "suppressed_canonical_job_id": relationship.suppressed_canonical_job_id,
                "suppressed_role_family": relationship.suppressed_role_family,
                "updated_at": relationship.updated_at,
            }
            if relationship is not None
            else {
                "state": "never_contacted",
                "suppression_scope": "none",
            }
        ),
    }


async def list_relationships(
    session: AsyncSession, *, profile_id: UUID, limit: int = 100
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(EmployerRelationship, CanonicalEmployer)
            .join(CanonicalEmployer, CanonicalEmployer.id == EmployerRelationship.employer_id)
            .where(EmployerRelationship.profile_id == profile_id)
            .order_by(desc(EmployerRelationship.last_interaction_at), CanonicalEmployer.id)
            .limit(min(max(limit, 1), 500))
        )
    ).all()
    return [
        {
            "employer_id": str(employer.id),
            "employer_name": employer.normalized_name,
            "primary_domain": employer.primary_domain,
            "state": _value(relationship.state),
            "suppression_scope": _value(relationship.suppression_scope),
            "suppression_reason": relationship.suppression_reason,
            "last_interaction_at": relationship.last_interaction_at,
            "updated_at": relationship.updated_at,
        }
        for relationship, employer in rows
    ]


async def get_history(
    session: AsyncSession,
    *,
    profile_id: UUID,
    employer_id: UUID,
    limit: int = 100,
) -> list[dict[str, Any]]:
    events = list(
        (
            await session.scalars(
                select(EmployerInteractionEvent)
                .where(
                    EmployerInteractionEvent.profile_id == profile_id,
                    EmployerInteractionEvent.employer_id == employer_id,
                )
                .order_by(
                    desc(EmployerInteractionEvent.occurred_at),
                    desc(EmployerInteractionEvent.created_at),
                )
                .limit(min(max(limit, 1), 500))
            )
        ).all()
    )
    return [
        {
            "id": str(event.id),
            "event_type": _value(event.event_type),
            "channel": _value(event.channel),
            "occurred_at": event.occurred_at,
            "application_id": event.application_id,
            "canonical_job_id": event.canonical_job_id,
            "source_job_id": event.source_job_id,
            "communication_session_id": event.communication_session_id,
            "turn_id": event.turn_id,
            "suppression_scope": _value(event.suppression_scope),
            "role_family": event.role_family,
            "evidence": event.event_metadata,
        }
        for event in events
    ]


__all__ = ["get_history", "get_relationship", "list_relationships"]
