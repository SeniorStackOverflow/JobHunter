from __future__ import annotations

# FastAPI's declarative dependency parameters intentionally call Depends.
# ruff: noqa: B008
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_actor
from app.database import get_session
from app.employers import EmployerRelationshipService
from app.employers.views import get_history, get_relationship, list_relationships
from app.models.entities import Application, EmailDelivery, EmployerContact
from app.models.enums import DeliveryStatus, SuppressionScope
from app.profiles import ProfileService

router = APIRouter(prefix="/employers", tags=["employers"])


class EmployerSuppressInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID | None = None
    scope: Literal["job", "role_family", "employer"]
    reason: str = Field(min_length=1, max_length=255)
    canonical_job_id: UUID | None = None
    role_family: str | None = Field(default=None, max_length=255)


class EmployerReopenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=255)


async def _profile_id(session: AsyncSession, value: UUID | None) -> UUID:
    profile = await ProfileService().get_profile(session, value)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return profile.id


@router.get("/relationships", dependencies=[Depends(require_api_actor)])
async def list_employer_relationships(
    profile_id: UUID | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    return await list_relationships(
        session, profile_id=await _profile_id(session, profile_id), limit=limit
    )


@router.get("/{employer_id}/relationship", dependencies=[Depends(require_api_actor)])
async def get_employer_relationship(
    employer_id: UUID,
    profile_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        return await get_relationship(
            session,
            profile_id=await _profile_id(session, profile_id),
            employer_id=employer_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="employer not found") from exc


@router.get("/{employer_id}/history", dependencies=[Depends(require_api_actor)])
async def get_employer_history(
    employer_id: UUID,
    profile_id: UUID | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    return await get_history(
        session,
        profile_id=await _profile_id(session, profile_id),
        employer_id=employer_id,
        limit=limit,
    )


@router.post("/{employer_id}/suppress", dependencies=[Depends(require_api_actor)])
async def suppress_employer_relationship(
    employer_id: UUID,
    payload: EmployerSuppressInput,
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    profile_id = await _profile_id(session, payload.profile_id)
    try:
        relationship = await EmployerRelationshipService().suppress(
            session,
            profile_id=profile_id,
            employer_id=employer_id,
            scope=SuppressionScope(payload.scope),
            reason=payload.reason,
            actor=actor,
            canonical_job_id=payload.canonical_job_id,
            role=payload.role_family,
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return {
        "employer_id": employer_id,
        "profile_id": profile_id,
        "state": relationship.state.value,
        "suppression_scope": relationship.suppression_scope.value,
    }


@router.post("/{employer_id}/reopen", dependencies=[Depends(require_api_actor)])
async def reopen_employer_relationship(
    employer_id: UUID,
    payload: EmployerReopenInput,
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    profile_id = await _profile_id(session, payload.profile_id)
    try:
        relationship = await EmployerRelationshipService().reopen(
            session,
            profile_id=profile_id,
            employer_id=employer_id,
            reason=payload.reason,
            actor=actor,
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return {
        "employer_id": employer_id,
        "profile_id": profile_id,
        "state": relationship.state.value,
        "suppression_scope": relationship.suppression_scope.value,
    }


@router.get("/delivery/failures", dependencies=[Depends(require_api_actor)])
async def list_delivery_failures(
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    statuses = {
        DeliveryStatus.BOUNCED_TRANSIENT,
        DeliveryStatus.BOUNCED_PERMANENT,
        DeliveryStatus.RECIPIENT_REJECTED,
        DeliveryStatus.MAILBOX_FULL,
        DeliveryStatus.DOMAIN_REJECTED,
        DeliveryStatus.POLICY_REJECTED,
        DeliveryStatus.SPAM_REJECTED,
        DeliveryStatus.DELIVERY_FAILED,
        DeliveryStatus.PERMANENT_FAILURE,
        DeliveryStatus.TEMPORARY_FAILURE,
    }
    rows = (
        await session.execute(
            select(EmailDelivery, Application, EmployerContact)
            .join(Application, Application.id == EmailDelivery.application_id)
            .join(EmployerContact, EmployerContact.id == Application.recipient_contact_id)
            .where(EmailDelivery.status.in_(statuses))
            .order_by(desc(EmailDelivery.updated_at))
            .limit(min(max(limit, 1), 500))
        )
    ).all()
    return [
        {
            "application_id": application.id,
            "employer_id": application.employer_id,
            "delivery_id": delivery.id,
            "provider": delivery.provider,
            "provider_message_id": delivery.provider_message_id,
            "status": delivery.status.value,
            "attempts": delivery.attempt_count,
            "last_attempt_at": delivery.last_attempt_at,
            "final_recipient": delivery.final_recipient or contact.value,
            "smtp_status": delivery.smtp_status,
            "failure_class": delivery.failure_class,
            "failure_reason": delivery.failure_reason,
            "bounced_at": delivery.bounced_at,
        }
        for delivery, application, contact in rows
    ]
