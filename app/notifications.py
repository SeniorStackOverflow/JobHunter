"""Keep source warnings tied to confirmed recovery, rather than navigation clicks."""

from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Alert, ScanRun
from app.models.enums import RunStatus

SOURCE_RECOVERY_CODES = {
    "adapter_degradation",
    "adapter_access_degraded",
    "mass_absence_suppressed",
}


def has_resolution(alert: Alert) -> bool:
    resolution = (alert.safe_diagnostics or {}).get("resolution")
    return isinstance(resolution, dict) and resolution.get("reason") == "source_recovered"


async def resolved_alert_ids(session: AsyncSession, alerts: Sequence[Alert]) -> set[UUID]:
    """Also recognize warnings repaired before automatic acknowledgement was introduced.

    Reads never modify alerts. An immutable successful scan started after the warning
    proves recovery; merely queueing a scan, a partial scan or current health does not.
    """
    resolved = {alert.id for alert in alerts if has_resolution(alert)}
    source_ids = {
        alert.source_id
        for alert in alerts
        if alert.source_id is not None and alert.code in SOURCE_RECOVERY_CODES
    }
    if not source_ids:
        return resolved
    rows = await session.execute(
        select(ScanRun.source_id, func.max(ScanRun.started_at))
        .where(
            ScanRun.source_id.in_(source_ids),
            ScanRun.status == RunStatus.SUCCEEDED,
            ScanRun.finished_at.is_not(None),
        )
        .group_by(ScanRun.source_id)
    )
    recovered_at: dict[UUID, datetime] = {
        source_id: started_at for source_id, started_at in rows.all() if started_at is not None
    }
    for alert in alerts:
        started_at = recovered_at.get(alert.source_id) if alert.source_id is not None else None
        if (
            alert.code in SOURCE_RECOVERY_CODES
            and started_at is not None
            and started_at.replace(tzinfo=UTC) >= alert.created_at.replace(tzinfo=UTC)
        ):
            resolved.add(alert.id)
    # A logical scan can be retried after a partial result while retaining its
    # original start time. Its own warning is resolved by that scan succeeding.
    scan_ids: dict[UUID, UUID] = {}
    for alert in alerts:
        if alert.code not in SOURCE_RECOVERY_CODES:
            continue
        with suppress(ValueError):
            scan_ids[alert.id] = UUID(str((alert.safe_diagnostics or {}).get("scan_id")))
    if scan_ids:
        scans = {
            run.id: run
            for run in (
                await session.scalars(
                    select(ScanRun).where(
                        ScanRun.id.in_(scan_ids.values()),
                        ScanRun.status == RunStatus.SUCCEEDED,
                        ScanRun.finished_at.is_not(None),
                    )
                )
            ).all()
        }
        for alert in alerts:
            run = scans.get(scan_ids[alert.id]) if alert.id in scan_ids else None
            if (
                run is not None
                and run.source_id == alert.source_id
                and run.finished_at is not None
                and run.finished_at.replace(tzinfo=UTC) >= alert.created_at.replace(tzinfo=UTC)
            ):
                resolved.add(alert.id)
    return resolved


async def unread_alerts(session: AsyncSession) -> list[Alert]:
    alerts = list(
        (
            await session.scalars(
                select(Alert)
                .where(Alert.acknowledged.is_(False))
                .order_by(Alert.created_at.desc(), Alert.id.desc())
            )
        ).all()
    )
    resolved = await resolved_alert_ids(session, alerts)
    return [alert for alert in alerts if alert.id not in resolved]


async def resolve_source_alerts(session: AsyncSession, run: ScanRun) -> None:
    """Participate in the successful scan transaction; never commit independently."""
    if run.status != RunStatus.SUCCEEDED or run.finished_at is None or run.started_at is None:
        return
    alerts = (
        await session.scalars(
            select(Alert).where(
                Alert.source_id == run.source_id,
                Alert.code.in_(SOURCE_RECOVERY_CODES),
                or_(
                    Alert.created_at <= run.started_at,
                    Alert.safe_diagnostics["scan_id"].as_string() == str(run.id),
                ),
                Alert.created_at <= run.finished_at,
            )
        )
    ).all()
    for alert in alerts:
        if has_resolution(alert):
            continue
        alert.acknowledged = True
        alert.safe_diagnostics = {
            **(alert.safe_diagnostics or {}),
            "resolution": {
                "reason": "source_recovered",
                "at": run.finished_at.isoformat(),
                "scan_id": str(run.id),
            },
        }
