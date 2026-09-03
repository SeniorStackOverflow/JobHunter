from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit import record_audit_event
from app.database.base import utcnow
from app.models.entities import CommunicationSession, PhoneDeviceSnapshot
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
EVENTS_GENERATION_KEY = "job-agent:phone:events:generation"

# A forced cursor replay right after a call re-emits that call's ``incoming_call``
# event, and the dedup guard must still recognise the session it already opened.
# But PhoneGate resets its event-id counter to 1 on restart, so a genuinely new
# low-id ``incoming_call`` must NOT be silenced by an unrelated historical session
# that happens to share the id. The guard therefore requires all three of: same
# event id, same generation, and a session opened inside this window — the real
# replay case (process died between commit and ``save_cursor``) satisfies all
# three; a genuine post-restart call fails either the generation clause (the loop
# saw the reset) or the time clause (it did not, so the colliding session is old).
_INCOMING_CALL_DEDUP_WINDOW = timedelta(minutes=10)


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
        self._generation = 0
        self._open_session_id: UUID | None = None
        self._cursor_seeded = False

    async def load_cursor(self) -> int | None:
        """Return the stored cursor, or ``None`` when the Redis key is absent.

        ``None`` lets the caller seed the cursor from ``status.latest_event_id``
        on first start instead of replaying PhoneGate's buffered history
        (spec §7.4).
        """
        raw = await self._redis.get(EVENTS_CURSOR_KEY)
        try:
            value = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            value = None
        self._cursor = value or 0
        if value is not None:
            self._cursor_seeded = True
        raw_gen = await self._redis.get(EVENTS_GENERATION_KEY)
        try:
            self._generation = int(raw_gen) if raw_gen is not None else 0
        except (TypeError, ValueError):
            self._generation = 0
        return value

    async def save_cursor(self, value: int) -> None:
        self._cursor = value
        self._cursor_seeded = True
        await self._redis.set(EVENTS_CURSOR_KEY, str(value))

    async def _bump_generation(self) -> None:
        self._generation += 1
        await self._redis.set(EVENTS_GENERATION_KEY, str(self._generation))

    @property
    def open_session_id(self) -> UUID | None:
        """Public read of the currently open session ID."""
        return self._open_session_id

    async def reconcile(self, status: DeviceStatus) -> None:
        """Bring local state in line with PhoneGate after a start or a resync."""
        async with self._session_factory() as session:
            open_row = await self._store.find_open(session)
            if open_row is None:
                self._open_session_id = None
                # A2 + finding #3: open a session on any active call state
                # (RINGING or an already-answered IN_CALL) with no open session.
                if status.call_state in {"RINGING", "IN_CALL"}:
                    correlation = await self._correlation.resolve(session, status.caller_number)
                    if correlation is not None:
                        normalized_address = (
                            normalize_e164(
                                status.caller_number,
                                region=self._settings.phone_caller_region,
                            )
                            or ""
                        )
                        answered = utcnow() if status.call_state == "IN_CALL" else None
                        call = await self._store.open(
                            session,
                            remote_raw=status.caller_number,
                            remote_address=normalized_address,
                            event_id=0,
                            correlation=correlation,
                            opened_at=utcnow(),
                            needs_review=True,
                            generation=self._generation,
                            answered_at=answered,
                            diagnostics={
                                "note": f"reconcile_opened_from_{status.call_state.lower()}",
                                "daemon_version": status.daemon_version,
                                "sim_operator": str(
                                    status.device.get("sim_operator")
                                    or status.device.get("operator")
                                    or ""
                                ),
                            },
                        )
                        self._open_session_id = call.id
                        # A3: audit the opened session
                        await record_audit_event(
                            session,
                            actor="phone-agent",
                            action="communication_session.opened",
                            entity_type="communication_session",
                            entity_id=str(call.id),
                            correlation_id=str(call.id),
                            details={
                                "caller": mask_phone(status.caller_number),
                                "application_id": (
                                    str(correlation.application_id)
                                    if correlation.application_id
                                    else None
                                ),
                                "profile_id": str(correlation.profile_id),
                            },
                        )
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
            await self._persist_health(status=None)
            logger.warning("phone_status_poll_failed", error_type=type(exc).__name__)
            return False

        self._health.record_status(status)

        # A1: seed cursor on first successful status if not yet seeded
        if not self._cursor_seeded:
            await self.save_cursor(status.latest_event_id)
            # finding #3: the boot-outage startup path seeds the cursor here
            # instead of in ``agent._run_loop`` — reconcile so an already-active
            # call is still opened.
            await self.reconcile(status)
            return status.call_state != "IDLE"

        try:
            page = await self._client.events(after_id=self._cursor, limit=250)
        except (PhoneGateUnavailable, PhoneGateError) as exc:
            self._health.record_transport_error(type(exc).__name__)
            await self._persist_health(status=status)
            logger.warning("phone_events_poll_failed", error_type=type(exc).__name__)
            return status.call_state != "IDLE"

        if page.latest_id < self._cursor:  # PhoneGate restarted / event log rotated
            logger.warning(
                "phone_events_reset",
                latest_id=page.latest_id,
                cursor=self._cursor,
                generation=self._generation,
            )
            await self._bump_generation()
            # Close any session still open from the previous generation — its transcript
            # ids will be reused by the new generation and would collide.
            async with self._session_factory() as session:
                open_row = await self._store.find_open(session)
                if open_row is not None:
                    await self._store.close(
                        session,
                        open_row,
                        outcome=CommunicationOutcome.UNKNOWN,
                        ended_at=utcnow(),
                        needs_review=True,
                        note="phonegate_generation_boundary",
                    )
                await session.commit()
            self._open_session_id = None
            # Re-read the fresh buffer from the start so a call that completed during the
            # restart window is still ingested.
            try:
                fresh = await self._client.events(after_id=0, limit=250)
            except (PhoneGateUnavailable, PhoneGateError) as exc:
                self._health.record_transport_error(type(exc).__name__)
                await self._persist_health(status=status)
                logger.warning("phone_events_poll_failed", error_type=type(exc).__name__)
                return status.call_state != "IDLE"
            dispatched_max = 0
            if fresh.events:
                ordered = sorted(fresh.events, key=lambda event: event.id)
                await self._dispatch_batch(ordered, status)
                dispatched_max = ordered[-1].id
            await self.reconcile(status)
            await self.save_cursor(max(fresh.latest_id, dispatched_max, 0))
        elif page.events:
            ordered = sorted(page.events, key=lambda event: event.id)
            if ordered[0].id > self._cursor + 1:
                logger.warning("phone_events_gap", first=ordered[0].id, cursor=self._cursor)
                await self._flag_open_session_gap()
            await self._dispatch_batch(ordered, status)
            await self.save_cursor(max(self._cursor, page.latest_id, ordered[-1].id))

        self._health.mark_poll_ok()
        await self._persist_health(status=status)
        return status.call_state != "IDLE"

    async def _persist_health(self, status: DeviceStatus | None) -> None:
        async with self._session_factory() as session:
            await self._health.persist(session)
            if status is not None:
                # Upsert device snapshot
                dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
                insert = pg_insert if dialect == "postgresql" else sqlite_insert
                payload = {
                    "connected": status.connected,
                    "mode": status.mode,
                    "daemon_version": status.daemon_version,
                    "battery": status.device.get("battery"),
                    "sim_operator": str(
                        status.device.get("sim_operator") or status.device.get("operator") or ""
                    ),
                    "rx_audio_stats": status.rx_audio_stats.model_dump(),
                    "call_state": status.call_state,
                    "caller_number": mask_phone(status.caller_number),
                }
                stmt = insert(PhoneDeviceSnapshot).values(
                    id="current",
                    payload=payload,
                    updated_at=utcnow(),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[PhoneDeviceSnapshot.id],
                    set_={
                        "payload": stmt.excluded.payload,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
                await session.execute(stmt)
            await session.commit()

    async def _flag_open_session_gap(self) -> None:
        async with self._session_factory() as session:
            open_row = await self._store.find_open(session)
            if open_row is not None:
                open_row.needs_review = True
                open_row.diagnostics = {**open_row.diagnostics, "note": "events_gap"}
                await session.commit()

    # ---- event dispatch -----------------------------------------------

    async def _dispatch_batch(self, ordered: list[PhoneEvent], status: DeviceStatus) -> None:
        """Dispatch events one at a time, each in its own savepoint, so a single
        malformed or failing event is logged and skipped without poisoning the
        session or stopping the batch (spec §4.2). The caller still advances the
        cursor past every event, poison included."""
        async with self._session_factory() as session:
            for event in ordered:
                try:
                    async with session.begin_nested():
                        await self._dispatch(session, event, status)
                except Exception as exc:
                    logger.warning(
                        "phone_event_dispatch_failed",
                        event_id=event.id,
                        event_type=event.type,
                        error=type(exc).__name__,
                    )
            await session.commit()

    async def _dispatch(
        self, session: AsyncSession, event: PhoneEvent, status: DeviceStatus
    ) -> None:
        if event.type == "incoming_call":
            await self._on_incoming_call(session, event, status)
        elif event.type == "call_state":
            await self._on_call_state(session, event, status)
        elif event.type == "transcript":
            await self._on_transcript(session, event, status)

    async def _on_incoming_call(
        self, session: AsyncSession, event: PhoneEvent, status: DeviceStatus
    ) -> None:
        recent_cutoff = utcnow() - _INCOMING_CALL_DEDUP_WINDOW
        already = await session.scalar(
            select(CommunicationSession.id).where(
                CommunicationSession.phonegate_event_id_start == event.id,
                CommunicationSession.phonegate_generation == self._generation,
                CommunicationSession.started_at >= recent_cutoff,
            )
        )
        if already is not None:
            return

        raw = str(event.data.get("caller_number") or "")
        # A5 minor: normalize once at the top; reuse for same-caller check and remote_address
        normalized_address = normalize_e164(raw, region=self._settings.phone_caller_region) or ""

        open_row = await self._store.find_open(session)
        if open_row is not None:
            if open_row.remote_raw == raw or (
                normalized_address and open_row.remote_address == normalized_address
            ):
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

        # A4: pass diagnostics with daemon_version and sim_operator
        call = await self._store.open(
            session,
            remote_raw=raw,
            remote_address=normalized_address,
            event_id=event.id,
            correlation=correlation,
            opened_at=utcnow(),
            generation=self._generation,
            diagnostics={
                "daemon_version": status.daemon_version,
                "sim_operator": str(
                    status.device.get("sim_operator") or status.device.get("operator") or ""
                ),
            },
        )
        self._open_session_id = call.id
        # A3: audit opened session
        await record_audit_event(
            session,
            actor="phone-agent",
            action="communication_session.opened",
            entity_type="communication_session",
            entity_id=str(call.id),
            correlation_id=str(call.id),
            details={
                "caller": mask_phone(raw),
                "application_id": (
                    str(correlation.application_id) if correlation.application_id else None
                ),
                "profile_id": str(correlation.profile_id),
            },
        )
        # A3: if correlation matched an application/contact, write a correlated event
        if correlation.application_id is not None or correlation.contact_id is not None:
            await record_audit_event(
                session,
                actor="phone-agent",
                action="communication_session.correlated",
                entity_type="communication_session",
                entity_id=str(call.id),
                correlation_id=str(call.id),
                details={
                    "application_id": (
                        str(correlation.application_id) if correlation.application_id else None
                    ),
                    "canonical_job_id": (
                        str(correlation.canonical_job_id) if correlation.canonical_job_id else None
                    ),
                    "contact_id": (str(correlation.contact_id) if correlation.contact_id else None),
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
        try:
            entry = TranscriptEntry.model_validate(payload)
        except ValidationError:
            logger.warning("phone_transcript_malformed", event_id=event.id, raw=repr(payload)[:200])
            return
        open_row = await self._store.find_open(session)
        if open_row is None:
            if status.call_state == "IDLE":
                logger.info("phone_transcript_after_call_end", transcript_id=entry.id)
                return
            correlation = await self._correlation.resolve(session, status.caller_number)
            if correlation is None:
                return
            # A5 minor: normalize E164 for remote_address
            normalized_address = (
                normalize_e164(status.caller_number, region=self._settings.phone_caller_region)
                or ""
            )
            # A4: pass diagnostics with daemon_version and sim_operator
            open_row = await self._store.open(
                session,
                remote_raw=status.caller_number,
                remote_address=normalized_address,
                event_id=event.id,
                correlation=correlation,
                opened_at=utcnow(),
                needs_review=True,
                generation=self._generation,
                diagnostics={
                    "note": "transcript_before_session_start",
                    "daemon_version": status.daemon_version,
                    "sim_operator": str(
                        status.device.get("sim_operator") or status.device.get("operator") or ""
                    ),
                },
            )
            self._open_session_id = open_row.id
            # A3: audit the opened session
            await record_audit_event(
                session,
                actor="phone-agent",
                action="communication_session.opened",
                entity_type="communication_session",
                entity_id=str(open_row.id),
                correlation_id=str(open_row.id),
                details={
                    "caller": mask_phone(status.caller_number),
                    "application_id": (
                        str(correlation.application_id) if correlation.application_id else None
                    ),
                    "profile_id": str(correlation.profile_id),
                },
            )
        await self._store.append_turn(session, session_id=open_row.id, entry=entry)
