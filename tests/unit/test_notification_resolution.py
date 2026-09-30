from datetime import UTC, datetime, timedelta

import pytest

from app.models.entities import Alert, JobSource, ScanRun
from app.models.enums import RunStatus, ScanType
from app.notifications import (
    has_resolution,
    resolve_source_alerts,
    resolved_alert_ids,
    unread_alerts,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(RunStatus))
async def test_only_completed_success_closes_the_matching_source_warnings(
    sqlite_session_factory, status
):
    now = datetime.now(UTC)
    async with sqlite_session_factory() as session:
        own = JobSource(name="Own", base_url="https://own.test", adapter_type="fixture")
        other = JobSource(name="Other", base_url="https://other.test", adapter_type="fixture")
        session.add_all([own, other])
        await session.flush()
        old = Alert(
            source_id=own.id,
            code="adapter_degradation",
            severity="high",
            message="Old failure",
            created_at=now - timedelta(hours=1),
            safe_diagnostics={"original": "kept"},
        )
        newer = Alert(
            source_id=own.id,
            code="adapter_access_degraded",
            severity="high",
            message="Concurrent failure",
            created_at=now + timedelta(minutes=1),
        )
        foreign = Alert(
            source_id=other.id,
            code="adapter_degradation",
            severity="high",
            message="Other failure",
            created_at=now - timedelta(hours=1),
        )
        unrelated = Alert(
            source_id=own.id,
            code="unknown_failure",
            severity="high",
            message="Unknown failure",
            created_at=now - timedelta(hours=1),
        )
        run = ScanRun(
            source_id=own.id,
            scan_type=ScanType.FULL,
            status=status,
            started_at=now,
            finished_at=now + timedelta(minutes=2),
        )
        session.add_all([old, newer, foreign, unrelated, run])
        await session.flush()
        await resolve_source_alerts(session, run)
        await session.commit()
        assert old.acknowledged is (status == RunStatus.SUCCEEDED)
        assert has_resolution(old) is (status == RunStatus.SUCCEEDED)
        assert old.safe_diagnostics["original"] == "kept"
        assert not any(a.acknowledged for a in [newer, foreign, unrelated])
        if status == RunStatus.SUCCEEDED:
            original = dict(old.safe_diagnostics)
            await resolve_source_alerts(session, run)
            assert old.safe_diagnostics == original


@pytest.mark.asyncio
async def test_legacy_recovery_is_recognized_without_writing_or_reopening_old_warnings(
    sqlite_session_factory,
):
    now = datetime.now(UTC)
    async with sqlite_session_factory() as session:
        source = JobSource(name="Source", base_url="https://source.test", adapter_type="fixture")
        session.add(source)
        await session.flush()
        old = Alert(
            source_id=source.id,
            code="adapter_degradation",
            severity="high",
            message="Recovered before this version",
            created_at=now - timedelta(days=3),
        )
        current = Alert(
            source_id=source.id,
            code="adapter_degradation",
            severity="high",
            message="New failure",
            created_at=now,
        )
        recovery = ScanRun(
            source_id=source.id,
            scan_type=ScanType.FULL,
            status=RunStatus.SUCCEEDED,
            started_at=now - timedelta(days=2),
            finished_at=now - timedelta(days=2) + timedelta(minutes=1),
        )
        session.add_all([old, current, recovery])
        await session.commit()
        assert await resolved_alert_ids(session, [old, current]) == {old.id}
        assert [a.id for a in await unread_alerts(session)] == [current.id]
        assert not old.acknowledged and not old.safe_diagnostics
        assert not session.dirty


@pytest.mark.asyncio
async def test_old_unresolved_warning_and_read_warning_do_not_mean_recovery(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        old = Alert(
            code="unknown_warning",
            severity="warning",
            message="Still unread",
            created_at=datetime.now(UTC) - timedelta(days=7),
        )
        read = Alert(
            code="unknown_warning", severity="warning", message="Read only", acknowledged=True
        )
        session.add_all([old, read])
        await session.commit()
        assert [a.id for a in await unread_alerts(session)] == [old.id]
        assert not await resolved_alert_ids(session, [old, read])


@pytest.mark.asyncio
async def test_successful_retry_resolves_its_own_warning_after_original_scan_start(
    sqlite_session_factory,
):
    now = datetime.now(UTC)
    async with sqlite_session_factory() as session:
        source = JobSource(name="Retry", base_url="https://retry.test", adapter_type="fixture")
        session.add(source)
        await session.flush()
        run = ScanRun(
            source_id=source.id,
            scan_type=ScanType.INCREMENTAL,
            status=RunStatus.SUCCEEDED,
            started_at=now - timedelta(hours=1),
            finished_at=now,
        )
        session.add(run)
        await session.flush()
        alert = Alert(
            source_id=source.id,
            code="adapter_access_degraded",
            severity="high",
            message="Before successful retry",
            created_at=now - timedelta(minutes=30),
            safe_diagnostics={"scan_id": str(run.id)},
        )
        session.add(alert)
        await session.flush()
        assert await resolved_alert_ids(session, [alert]) == {alert.id}
        await resolve_source_alerts(session, run)
        await session.commit()
        assert alert.acknowledged and has_resolution(alert)
