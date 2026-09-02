from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.models.entities import CommunicationSession
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
)
from app.phone.correlation import CorrelationResult


class SessionStore:
    async def find_open(self, session: AsyncSession) -> CommunicationSession | None:
        return cast(
            CommunicationSession | None,
            await session.scalar(
                select(CommunicationSession)
                .where(CommunicationSession.ended_at.is_(None))
                .order_by(CommunicationSession.started_at.desc())
                .limit(1)
            ),
        )

    async def open(
        self,
        session: AsyncSession,
        *,
        remote_raw: str,
        remote_address: str,
        event_id: int,
        correlation: CorrelationResult,
        opened_at: datetime,
        needs_review: bool = False,
        note: str | None = None,
    ) -> CommunicationSession:
        call = CommunicationSession(
            profile_id=correlation.profile_id,
            application_id=correlation.application_id,
            canonical_job_id=correlation.canonical_job_id,
            source_job_id=correlation.source_job_id,
            contact_id=correlation.contact_id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address=remote_address,
            remote_raw=remote_raw,
            phonegate_event_id_start=event_id,
            started_at=opened_at,
            ringing_at=opened_at,
            needs_review=needs_review,
            diagnostics={"note": note} if note else {},
        )
        session.add(call)
        await session.flush()
        return call

    async def touch_ringing(self, call: CommunicationSession, when: datetime) -> None:
        if call.ringing_at is None:
            call.ringing_at = when

    async def touch_answered(self, call: CommunicationSession, when: datetime) -> None:
        if call.answered_at is None:
            call.answered_at = when

    async def close(
        self,
        session: AsyncSession,
        call: CommunicationSession,
        *,
        outcome: CommunicationOutcome,
        ended_at: datetime,
        needs_review: bool = False,
        rx_stats: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> None:
        call.ended_at = ended_at
        call.outcome = outcome
        if needs_review:
            call.needs_review = True
        if rx_stats is not None:
            call.rx_frame_stats = rx_stats
        if note:
            call.diagnostics = {**call.diagnostics, "close_note": note}
        call.updated_at = utcnow()
        await session.flush()
