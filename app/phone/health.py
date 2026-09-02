from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.models.entities import PhoneChannelHealth
from app.models.enums import PhoneComponentStatus
from app.phone.schemas import DeviceStatus

_RANK = {
    PhoneComponentStatus.HEALTHY: 0,
    PhoneComponentStatus.UNKNOWN: 1,
    PhoneComponentStatus.DEGRADED: 2,
    PhoneComponentStatus.UNAVAILABLE: 3,
}
_CHANNEL_COMPONENTS = ("phonegate_transport", "a14_daemon", "gsm_line", "agent")


@dataclass(slots=True)
class HealthComponent:
    component: str
    status: PhoneComponentStatus
    detail: str | None = None
    last_ok_at: datetime | None = None


def channel_status(components: list[HealthComponent]) -> PhoneComponentStatus:
    relevant = [c for c in components if c.component in _CHANNEL_COMPONENTS]
    if not relevant:
        return PhoneComponentStatus.UNKNOWN
    return max(relevant, key=lambda c: _RANK[c.status]).status


class HealthTracker:
    def __init__(self) -> None:
        self._components: dict[str, HealthComponent] = {}

    def _set(self, name: str, status: PhoneComponentStatus, detail: str | None = None) -> None:
        prior = self._components.get(name)
        last_ok = prior.last_ok_at if prior else None
        if status is PhoneComponentStatus.HEALTHY:
            last_ok = utcnow()
        self._components[name] = HealthComponent(name, status, detail, last_ok)

    def mark_poll_ok(self) -> None:
        self._set("agent", PhoneComponentStatus.HEALTHY, "poll ok")

    def record_transport_error(self, name: str) -> None:
        self._set("phonegate_transport", PhoneComponentStatus.UNAVAILABLE, name)
        self._set("a14_daemon", PhoneComponentStatus.UNKNOWN, "no status")
        self._set("gsm_line", PhoneComponentStatus.UNKNOWN, "no status")

    def record_status(self, status: DeviceStatus) -> None:
        self._set("phonegate_transport", PhoneComponentStatus.HEALTHY, None)
        if status.is_daemon_mode:
            self._set("a14_daemon", PhoneComponentStatus.HEALTHY, "Zero-ADB")
        elif status.connected:
            self._set("a14_daemon", PhoneComponentStatus.DEGRADED, "ADB fallback")
        else:
            self._set("a14_daemon", PhoneComponentStatus.UNAVAILABLE, "not connected")
        self._set(
            "gsm_line",
            PhoneComponentStatus.HEALTHY if status.connected else PhoneComponentStatus.UNAVAILABLE,
            f"call_state={status.call_state}",
        )
        operator = str(status.device.get("sim_operator") or status.device.get("operator") or "")
        self._set(
            "sim_account",
            PhoneComponentStatus.HEALTHY if operator else PhoneComponentStatus.UNKNOWN,
            operator or "no telemetry",
        )

    def components(self) -> list[HealthComponent]:
        return list(self._components.values())

    async def persist(self, session: AsyncSession) -> None:
        dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        now = utcnow()
        for component in self._components.values():
            stmt = insert(PhoneChannelHealth).values(
                component=component.component,
                status=component.status,
                detail=component.detail,
                last_ok_at=component.last_ok_at,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[PhoneChannelHealth.component],
                set_={
                    "status": stmt.excluded.status,
                    "detail": stmt.excluded.detail,
                    "last_ok_at": stmt.excluded.last_ok_at,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)
