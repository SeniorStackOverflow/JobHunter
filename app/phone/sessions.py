from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.models.entities import CommunicationSession, CommunicationTurn
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    TurnSpeaker,
)
from app.phone.correlation import CorrelationResult
from app.phone.schemas import TranscriptEntry


def speaker_from_phonegate(value: str) -> TurnSpeaker:
    return {"rx": TurnSpeaker.EMPLOYER, "tx": TurnSpeaker.OPERATOR}.get(value, TurnSpeaker.SYSTEM)


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
        diagnostics: dict[str, Any] | None = None,
    ) -> CommunicationSession:
        # A4: merge diagnostics dict (if provided) with note handling
        call_diagnostics: dict[str, Any] = {}
        if note:
            call_diagnostics["note"] = note
        if diagnostics:
            call_diagnostics.update(diagnostics)

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
            diagnostics=call_diagnostics,
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

    async def append_turn(
        self,
        session: AsyncSession,
        *,
        session_id: UUID,
        entry: TranscriptEntry,
    ) -> CommunicationTurn | None:
        exists = await session.scalar(
            select(CommunicationTurn.id).where(
                CommunicationTurn.session_id == session_id,
                CommunicationTurn.phonegate_transcript_id == entry.id,
            )
        )
        if exists is not None:
            return None
        count = await session.scalar(
            select(func.count(CommunicationTurn.id)).where(
                CommunicationTurn.session_id == session_id
            )
        )
        turn = CommunicationTurn(
            session_id=session_id,
            phonegate_transcript_id=entry.id,
            seq=int(count or 0) + 1,
            speaker=speaker_from_phonegate(entry.speaker),
            text=entry.text,
            raw_text=entry.text,
            asr_backend=entry.backend or None,
            asr_confidence=entry.confidence,
            asr_meta=entry.meta or None,
            occurred_at=datetime.fromtimestamp(entry.timestamp_ms / 1000, tz=UTC)
            if entry.timestamp_ms
            else datetime.now(UTC),
        )
        session.add(turn)
        await session.flush()
        return turn
