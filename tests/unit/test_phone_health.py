from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import PhoneChannelHealth
from app.models.enums import PhoneComponentStatus
from app.phone.health import HealthTracker, channel_status
from app.phone.schemas import DeviceStatus


def _status(**kw: object) -> DeviceStatus:
    base = {
        "connected": True,
        "mode": "Zero-ADB",
        "call_state": "IDLE",
        "rx_audio_stats": {},
        "device": {"sim_operator": "Orange"},
    }
    base.update(kw)
    return DeviceStatus.model_validate(base)


def test_healthy_when_daemon_connected() -> None:
    tracker = HealthTracker()
    tracker.mark_poll_ok()
    tracker.record_status(_status())
    by_name = {c.component: c for c in tracker.components()}
    assert by_name["phonegate_transport"].status is PhoneComponentStatus.HEALTHY
    assert by_name["a14_daemon"].status is PhoneComponentStatus.HEALTHY
    assert channel_status(tracker.components()) is PhoneComponentStatus.HEALTHY


def test_transport_error_is_unavailable() -> None:
    tracker = HealthTracker()
    tracker.record_transport_error("ConnectError")
    by_name = {c.component: c for c in tracker.components()}
    assert by_name["phonegate_transport"].status is PhoneComponentStatus.UNAVAILABLE
    assert channel_status(tracker.components()) is PhoneComponentStatus.UNAVAILABLE


def test_adb_fallback_is_degraded() -> None:
    tracker = HealthTracker()
    tracker.mark_poll_ok()
    tracker.record_status(_status(mode="ADB fallback"))
    by_name = {c.component: c for c in tracker.components()}
    assert by_name["a14_daemon"].status is PhoneComponentStatus.DEGRADED


@pytest.mark.asyncio
async def test_persist_upserts_rows(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tracker = HealthTracker()
    tracker.mark_poll_ok()
    tracker.record_status(_status())
    async with sqlite_session_factory() as session:
        await tracker.persist(session)
        await session.commit()
        await tracker.persist(session)  # second upsert, no duplicate PK
        await session.commit()
        rows = (await session.scalars(select(PhoneChannelHealth))).all()
    assert {r.component for r in rows} >= {"phonegate_transport", "a14_daemon", "agent"}
