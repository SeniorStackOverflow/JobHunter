from __future__ import annotations

from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit import record_audit_event
from app.database.base import utcnow
from app.models.entities import CommunicationSession
from app.models.enums import CommunicationOutcome
from app.phone.client import PhoneGateClient, PhoneGateError, PhoneGateUnavailable
from app.phone.correlation import CallerCorrelation
from app.phone.health import HealthTracker
from app.phone.numbers import mask_phone, normalize_e164
from app.phone.schemas import DeviceStatus, PhoneEvent, TranscriptEntry
from app.phone.sessions import SessionStore
from app.settings.config import Settings

logger = structlog.get_logger(__name__)

EVENTS_CURSOR_KEY = "job-agent:phone:events:cursor"


class IngestLoop:
    def __init__(
        self,
        *,
        client: PhoneGateClient,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        correlation: CallerCorrelation,
        health: HealthTracker,
        settings: Settings,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._redis = redis
        self._correlation = correlation
        self._health = health
        self._settings = settings
        self._store = SessionStore()
        self._cursor = 0
        self._open_session_id: UUID | None = None

    async def load_cursor(self) -> int:
        raw = await self._redis.get(EVENTS_CURSOR_KEY)
        try:
            self._cursor = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            self._cursor = 0
        return self._cursor

    async def save_cursor(self, value: int) -> None:
        self._cursor = value
        await self._redis.set(EVENTS_CURSOR_KEY, str(value))

    async def reconcile(self, status: DeviceStatus) -> None:
        """Bring local state in line with PhoneGate after a start or a resync."""
        async with self._session_factory() as session:
            open_row = await self._store.find_open(session)
            if open_row is None:
                self._open_session_id = None
            elif status.call_state == "IDLE":
                await self._store.close(
                    session,
                    open_row,
                    outcome=CommunicationOutcome.UNKNOWN,
                    ended_at=utcnow(),
                    needs_review=True,
                    note="reconcile_closed_no_active_call",
                )
                self._open_session_id = None
            else:
                self._open_session_id = open_row.id
                open_row.diagnostics = {
                    **open_row.diagnostics,
                    "reconciled_at": utcnow().isoformat(),
                }
            await session.commit()

    # ---- poll cycle -----------------------------------------------------

    async def run_cycle(self) -> bool:
        """Run one poll iteration; return True when a call is currently active."""
        try:
            status = await self._client.device_status()
        except (PhoneGateUnavailable, PhoneGateError) as exc:
            self._health.record_transport_error(type(exc).__name__)
            await self._persist_health()
            logger.warning("phone_status_poll_failed", error_type=type(exc).__name__)
            return False

        self._health.record_status(status)

        try:
            page = await self._client.events(after_id=self._cursor, limit=250)
        except (PhoneGateUnavailable, PhoneGateError) as exc:
            self._health.record_transport_error(type(exc).__name__)
            await self._persist_health()
            return status.call_state != "IDLE"

        if page.latest_id < self._cursor:  # PhoneGate restarted / event log rotated
            logger.warning("phone_events_reset", latest_id=page.latest_id, cursor=self._cursor)
            await self.reconcile(status)
            await self.save_cursor(page.latest_id)
        elif page.events:
            ordered = sorted(page.events, key=lambda event: event.id)
            if ordered[0].id > self._cursor + 1:
                logger.warning("phone_events_gap", first=ordered[0].id, cursor=self._cursor)
                await self._flag_open_session_gap()
            async with self._session_factory() as session:
                for event in ordered:
                    await self._dispatch(session, event, status)
                await session.commit()
            await self.save_cursor(max(self._cursor, page.latest_id, ordered[-1].id))

        self._health.mark_poll_ok()
        await self._persist_health()
        return status.call_state != "IDLE"

    async def _persist_health(self) -> None:
        async with self._session_factory() as session:
            await self._health.persist(session)
            await session.commit()

    async def _flag_open_session_gap(self) -> None:
        async with self._session_factory() as session:
            open_row = await self._store.find_open(session)
            if open_row is not None:
                open_row.needs_review = True
                open_row.diagnostics = {**open_row.diagnostics, "note": "events_gap"}
                await session.commit()

    # ---- event dispatch -----------------------------------------------

    async def _dispatch(
        self, session: AsyncSession, event: PhoneEvent, status: DeviceStatus
    ) -> None:
        if event.type == "incoming_call":
            await self._on_incoming_call(session, event)
        elif event.type == "call_state":
            await self._on_call_state(session, event, status)
        elif event.type == "transcript":
            await self._on_transcript(session, event, status)

    async def _on_incoming_call(self, session: AsyncSession, event: PhoneEvent) -> None:
        already = await session.scalar(
            select(CommunicationSession.id).where(
                CommunicationSession.phonegate_event_id_start == event.id
            )
        )
        if already is not None:
            return

        raw = str(event.data.get("caller_number") or "")
        open_row = await self._store.find_open(session)
        if open_row is not None:
            if open_row.remote_raw == raw or (raw and open_row.remote_address == raw):
                return
            await self._store.close(
                session,
                open_row,
                outcome=CommunicationOutcome.ABANDONED,
                ended_at=utcnow(),
                note="superseded_by_new_caller",
            )

        correlation = await self._correlation.resolve(session, raw)
        if correlation is None:
            logger.error("phone_no_default_profile", caller=mask_phone(raw))
            return

        call = await self._store.open(
            session,
            remote_raw=raw,
            remote_address=normalize_e164(raw, region=self._settings.phone_caller_region) or "",
            event_id=event.id,
            correlation=correlation,
            opened_at=utcnow(),
        )
        self._open_session_id = call.id
        await record_audit_event(
            session,
            actor="phone-agent",
            action="communication_session.opened",
            entity_type="communication_session",
            entity_id=str(call.id),
            correlation_id=str(call.id),
            details={
                "caller": mask_phone(raw),
                "application_id": str(correlation.application_id),
                "profile_id": str(correlation.profile_id),
            },
        )

    async def _on_call_state(
        self, session: AsyncSession, event: PhoneEvent, status: DeviceStatus
    ) -> None:
        state = str(event.data.get("state") or "")
        open_row = await self._store.find_open(session)
        if open_row is None:
            return
        if state == "RINGING":
            await self._store.touch_ringing(open_row, utcnow())
        elif state == "IN_CALL":
            await self._store.touch_answered(open_row, utcnow())
        elif state == "IDLE":
            outcome = (
                CommunicationOutcome.COMPLETED
                if open_row.answered_at is not None
                else CommunicationOutcome.MISSED
            )
            await self._store.close(
                session,
                open_row,
                outcome=outcome,
                ended_at=utcnow(),
                rx_stats=status.rx_audio_stats.model_dump(),
            )
            self._open_session_id = None

    async def _on_transcript(
        self, session: AsyncSession, event: PhoneEvent, status: DeviceStatus
    ) -> None:
        payload = event.data.get("transcript")
        if not isinstance(payload, dict):
            return
        entry = TranscriptEntry.model_validate(payload)
        open_row = await self._store.find_open(session)
        if open_row is None:
            if status.call_state == "IDLE":
                logger.info("phone_transcript_after_call_end", transcript_id=entry.id)
                return
            correlation = await self._correlation.resolve(session, status.caller_number)
            if correlation is None:
                return
            open_row = await self._store.open(
                session,
                remote_raw=status.caller_number,
                remote_address="",
                event_id=event.id,
                correlation=correlation,
                opened_at=utcnow(),
                needs_review=True,
                note="transcript_before_session_start",
            )
            self._open_session_id = open_row.id
        await self._store.append_turn(session, session_id=open_row.id, entry=entry)
