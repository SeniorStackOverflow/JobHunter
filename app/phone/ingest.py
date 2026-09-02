from __future__ import annotations

from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.base import utcnow
from app.models.enums import CommunicationOutcome
from app.phone.client import PhoneGateClient
from app.phone.correlation import CallerCorrelation
from app.phone.health import HealthTracker
from app.phone.schemas import DeviceStatus
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
