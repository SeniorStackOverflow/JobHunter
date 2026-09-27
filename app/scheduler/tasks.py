# mypy: disable-error-code="untyped-decorator"
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Never, cast
from uuid import UUID

import structlog
from celery import Task
from celery.schedules import crontab
from redis import Redis
from sqlalchemy import and_, or_, select

from app.crawlers.pipeline import (
    ScanService,
    scan_has_pending_reference_failures,
    scan_resume_is_stalled,
)
from app.crawlers.registry import build_default_registry
from app.database import async_session_factory
from app.models.entities import JobSource, ScanRun
from app.models.enums import RunStatus, ScanType, SourceHealth
from app.observability import bind_log_context
from app.observability.metrics import (
    RABOTA_PROXY_PRIMARY_REACHABLE,
    SCAN_ERRORS,
    SCAN_JOBS,
    SCAN_RUNS,
    SOURCE_HEALTH,
)
from app.scheduler.celery_app import celery_app
from app.scheduler.locks import (
    close_redis_client,
    leased_redis_lock,
    lock_key,
    reserve_once,
)
from app.settings import get_settings

logger = structlog.get_logger(__name__)
_task_event_loop: asyncio.AbstractEventLoop | None = None

MAX_AUTO_RESUME_DEPTH = 5

DEFAULT_SOURCE_SCHEDULES = {
    "incremental": "0 */2 * * *",
    "recheck": "0 2 * * *",
    "full": "0 3 * * 0",
}
_CONFIG_SECTION = {
    "incremental": "incremental_scan",
    "recheck": "active_job_recheck",
    "full": "full_scan",
}
_SOURCE_STATES = tuple(item.value for item in SourceHealth)
_RABOTA_DEGRADED_RECOVERY_INTERVAL_HOURS = 1


@dataclass(frozen=True, slots=True)
class SourceSchedule:
    source_id: UUID
    adapter_type: str
    configuration: dict[str, Any]
    health_status: SourceHealth
    has_successful_full_scan: bool


def _run_async[ResultT](awaitable: Coroutine[Any, Any, ResultT]) -> ResultT:
    global _task_event_loop
    if _task_event_loop is None or _task_event_loop.is_closed():
        _task_event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_task_event_loop)
    return _task_event_loop.run_until_complete(awaitable)


def close_task_event_loop() -> None:
    global _task_event_loop
    if _task_event_loop is None or _task_event_loop.is_closed():
        _task_event_loop = None
        return
    from app.database import engine

    _task_event_loop.run_until_complete(engine.dispose())
    _task_event_loop.close()
    _task_event_loop = None


def _redis_client() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


def _scan_service() -> ScanService:
    return ScanService(async_session_factory, build_default_registry())


def _parse_uuid(raw_value: str, *, name: str) -> UUID:
    try:
        return UUID(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a valid UUID") from exc


def _configured_schedule(source: SourceSchedule, operation: str) -> str:
    section_name = _CONFIG_SECTION[operation]
    roots: list[dict[str, Any]] = [source.configuration]
    nested = source.configuration.get("source")
    if isinstance(nested, dict):
        roots.append(nested)
    for root in roots:
        section = root.get(section_name)
        if isinstance(section, dict):
            expression = section.get("schedule")
            if isinstance(expression, str) and expression.strip():
                return expression.strip()
    return DEFAULT_SOURCE_SCHEDULES[operation]


def cron_expression_is_due(expression: str, now: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron schedule must contain exactly five fields")
    minute, hour, day_of_month, month_of_year, day_of_week = fields
    current = now.astimezone(UTC).replace(microsecond=0)
    schedule = crontab(
        minute=minute,
        hour=hour,
        day_of_week=day_of_week,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        nowfun=lambda: current,
    )
    previous_minute = current.replace(second=0) - timedelta(minutes=1)
    return bool(schedule.is_due(previous_minute).is_due)


async def _load_enabled_sources() -> list[SourceSchedule]:
    async with async_session_factory() as session:
        sources = list(
            (
                await session.scalars(
                    select(JobSource).where(
                        JobSource.enabled.is_(True),
                        JobSource.health_status.notin_(
                            [SourceHealth.PAUSED, SourceHealth.DISABLED]
                        ),
                    )
                )
            ).all()
        )
        successful_full_source_ids = set(
            (
                await session.scalars(
                    select(ScanRun.source_id).where(
                        ScanRun.scan_type == ScanType.FULL,
                        ScanRun.status == RunStatus.SUCCEEDED,
                    )
                )
            ).all()
        )
    return [
        SourceSchedule(
            source_id=source.id,
            adapter_type=source.adapter_type,
            configuration=dict(source.configuration),
            health_status=source.health_status,
            has_successful_full_scan=source.id in successful_full_source_ids,
        )
        for source in sources
    ]


def _resume_from_checkpoint_enabled(source: SourceSchedule, operation: str) -> bool:
    section_name = _CONFIG_SECTION[operation]
    roots: list[dict[str, Any]] = [source.configuration]
    nested = source.configuration.get("source")
    if isinstance(nested, dict):
        roots.append(nested)
    for root in roots:
        section = root.get(section_name)
        if isinstance(section, dict):
            value = section.get("resume_from_checkpoint")
            if isinstance(value, bool):
                return value
    return False


async def _get_or_create_queued_scan(
    source_id: UUID,
    scan_type: ScanType,
    *,
    resume_from_checkpoint: bool = False,
) -> tuple[ScanRun, bool]:
    async with async_session_factory() as session:
        running = await session.scalar(
            select(ScanRun)
            .where(
                ScanRun.source_id == source_id,
                ScanRun.scan_type == scan_type,
                ScanRun.status == RunStatus.RUNNING,
            )
            .order_by(ScanRun.started_at.desc().nullslast())
            .limit(1)
        )
        if running is not None:
            return running, False
        queued = await session.scalar(
            select(ScanRun)
            .where(
                ScanRun.source_id == source_id,
                ScanRun.scan_type == scan_type,
                ScanRun.status == RunStatus.QUEUED,
            )
            .order_by(ScanRun.started_at.desc().nullslast())
            .limit(1)
        )
    if queued is not None:
        return queued, True
    run = await _scan_service().create_scan(
        source_id,
        scan_type,
        actor="celery_beat",
        resume_from_checkpoint=resume_from_checkpoint,
    )
    return run, run.status != RunStatus.RUNNING


async def _load_scan(scan_id: UUID) -> ScanRun:
    async with async_session_factory() as session:
        run = await session.get(ScanRun, scan_id)
        if run is None:
            raise LookupError(f"scan {scan_id} does not exist")
        return run


async def _scan_identity(scan_id: UUID) -> tuple[UUID, ScanType]:
    async with async_session_factory() as session:
        run = await session.get(ScanRun, scan_id)
        if run is None:
            raise LookupError(f"scan {scan_id} does not exist")
        return run.source_id, run.scan_type


def _scan_stale(run: ScanRun, cutoff: datetime) -> bool:
    heartbeat = run.heartbeat_at or run.started_at
    if heartbeat is None:
        return True
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    return heartbeat < cutoff


async def _reconcile_orphaned_scans(
    client: Redis,
    *,
    source_id: UUID | None = None,
    scan_type: ScanType | None = None,
    limit: int = 100,
    now: datetime | None = None,
) -> list[ScanRun]:
    settings = get_settings()
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(seconds=settings.crawler_scan_heartbeat_stale_seconds)
    conditions = [
        ScanRun.status == RunStatus.RUNNING,
        or_(
            ScanRun.heartbeat_at < cutoff,
            and_(ScanRun.heartbeat_at.is_(None), ScanRun.started_at < cutoff),
            and_(ScanRun.heartbeat_at.is_(None), ScanRun.started_at.is_(None)),
        ),
    ]
    if source_id is not None:
        conditions.append(ScanRun.source_id == source_id)
    if scan_type is not None:
        conditions.append(ScanRun.scan_type == scan_type)

    async with async_session_factory() as session:
        candidates = (
            await session.scalars(
                select(ScanRun)
                .where(*conditions)
                .order_by(ScanRun.started_at.asc().nullsfirst())
                .limit(limit)
            )
        ).all()

    reconciled: list[ScanRun] = []
    for candidate in candidates:
        lease_key = lock_key("source-operation", str(candidate.source_id))
        if client.exists(lease_key):
            logger.warning(
                "scan_stalled_with_live_owner",
                scan_id=str(candidate.id),
                source_id=str(candidate.source_id),
                scan_type=candidate.scan_type.value,
                owner_task_id=candidate.owner_task_id,
                heartbeat_at=(
                    candidate.heartbeat_at.isoformat() if candidate.heartbeat_at else None
                ),
            )
            continue

        async with async_session_factory() as session:
            run = await session.scalar(
                select(ScanRun).where(ScanRun.id == candidate.id).with_for_update(skip_locked=True)
            )
            if run is None or run.status != RunStatus.RUNNING or not _scan_stale(run, cutoff):
                continue
            if client.exists(lock_key("source-operation", str(run.source_id))):
                continue

            owner_task_id = run.owner_task_id
            last_heartbeat = run.heartbeat_at or run.started_at
            diagnostics = dict(run.diagnostics or {})
            diagnostics.update(
                {
                    "failure": "orphaned_worker",
                    "orphaned_owner_task_id": owner_task_id,
                    "orphaned_heartbeat_at": (
                        last_heartbeat.isoformat() if last_heartbeat else None
                    ),
                    "orphaned_at": current.isoformat(),
                }
            )
            run.diagnostics = diagnostics
            run.status = RunStatus.PARTIAL
            run.finished_at = current
            run.heartbeat_at = current
            run.owner_task_id = None
            source = await session.get(JobSource, run.source_id)
            if source is not None:
                source.last_scan_status = RunStatus.PARTIAL
            from app.audit import record_audit_event

            await record_audit_event(
                session,
                actor="scan_reconciler",
                action="scan.orphan_reconciled",
                entity_type="scan_run",
                entity_id=str(run.id),
                correlation_id=str(run.id),
                decision="partial",
                details={
                    "owner_task_id": owner_task_id,
                    "last_heartbeat": (last_heartbeat.isoformat() if last_heartbeat else None),
                },
            )
            await session.commit()
            reconciled.append(run)
            logger.warning(
                "scan_orphan_reconciled",
                scan_id=str(run.id),
                source_id=str(run.source_id),
                scan_type=run.scan_type.value,
                owner_task_id=owner_task_id,
                last_heartbeat=(last_heartbeat.isoformat() if last_heartbeat else None),
            )
    return reconciled


async def _run_scan_with_timeout(
    service: ScanService,
    scan_id: UUID,
    *,
    owner_task_id: str,
    timeout_seconds: int,
) -> ScanRun:
    try:
        async with asyncio.timeout(timeout_seconds):
            return await service.run_scan(scan_id)
    except TimeoutError:
        return await service.interrupt_scan(
            scan_id,
            reason="runtime_timeout",
            expected_owner_task_id=owner_task_id,
            details={"timeout_seconds": timeout_seconds},
        )


def _scan_timeout_seconds(scan_type: ScanType) -> int:
    settings = get_settings()
    if scan_type == ScanType.INCREMENTAL:
        return settings.crawler_incremental_scan_timeout_seconds
    return settings.crawler_full_scan_timeout_seconds


def _scan_result_payload(run: ScanRun) -> dict[str, Any]:
    return {
        "scan_id": str(run.id),
        "source_id": str(run.source_id),
        "scan_type": run.scan_type.value,
        "status": run.status.value,
        "found_jobs": run.found_jobs,
        "new_jobs": run.new_jobs,
        "updated_jobs": run.updated_jobs,
        "unchanged_jobs": run.unchanged_jobs,
        "errors": run.parsing_errors + run.network_errors,
        "processing_task_id": None,
        "resume_scan_id": None,
    }


def _record_scan_result(run: ScanRun) -> None:
    SCAN_RUNS.labels(run.scan_type.value, run.status.value).inc()
    SCAN_JOBS.labels("new").inc(run.new_jobs)
    SCAN_JOBS.labels("updated").inc(run.updated_jobs)
    SCAN_JOBS.labels("unchanged").inc(run.unchanged_jobs)
    SCAN_ERRORS.labels("parsing").inc(run.parsing_errors)
    SCAN_ERRORS.labels("network").inc(run.network_errors)


async def _source_health(source_id: UUID) -> SourceHealth | None:
    async with async_session_factory() as session:
        return cast(
            SourceHealth | None,
            await session.scalar(select(JobSource.health_status).where(JobSource.id == source_id)),
        )


async def _downstream_actions_allowed(source_id: UUID) -> bool:
    async with async_session_factory() as session:
        paused = await session.scalar(
            select(JobSource.automatic_actions_paused).where(JobSource.id == source_id)
        )
        return paused is False


@dataclass(frozen=True, slots=True)
class RecheckPolicy:
    close_after_confirmed_absence_count: int = 3
    max_jobs_per_run: int = 300
    min_recheck_interval_hours: int = 20


def _recheck_policy_from_configuration(configuration: object) -> RecheckPolicy:
    raw = configuration if isinstance(configuration, dict) else {}
    nested = raw.get("source")
    roots = [raw, nested] if isinstance(nested, dict) else [raw]
    for root in roots:
        section = root.get("active_job_recheck")
        if not isinstance(section, dict):
            continue
        threshold = section.get("close_after_confirmed_absence_count")
        max_jobs = section.get("max_jobs_per_run")
        min_interval = section.get("min_recheck_interval_hours")
        return RecheckPolicy(
            close_after_confirmed_absence_count=(
                threshold if isinstance(threshold, int) and 1 <= threshold <= 100 else 3
            ),
            max_jobs_per_run=(
                max_jobs if isinstance(max_jobs, int) and 1 <= max_jobs <= 10_000 else 300
            ),
            min_recheck_interval_hours=(
                min_interval if isinstance(min_interval, int) and 0 <= min_interval <= 168 else 20
            ),
        )
    return RecheckPolicy()


async def _recheck_policy(source_id: UUID) -> RecheckPolicy:
    async with async_session_factory() as session:
        configuration = await session.scalar(
            select(JobSource.configuration).where(JobSource.id == source_id)
        )
    return _recheck_policy_from_configuration(configuration)


def _set_source_health_metric(source_id: UUID, current: SourceHealth | None) -> None:
    if current is None:
        return
    for state in _SOURCE_STATES:
        SOURCE_HEALTH.labels(str(source_id), state).set(1 if state == current.value else 0)


def _mem_available_mb(meminfo_path: Path = Path("/proc/meminfo")) -> int | None:
    try:
        for line in meminfo_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) // 1024
    except (OSError, ValueError):
        return None
    return None


def _browser_partial_resume_delay_seconds(run: ScanRun, base_seconds: int) -> int:
    diagnostics = run.diagnostics if isinstance(run.diagnostics, dict) else {}
    errors = diagnostics.get("errors")
    if not isinstance(errors, list):
        return 60
    browser_failure = any(
        isinstance(item, dict)
        and isinstance(item.get("reason"), str)
        and item["reason"].startswith("browser_")
        for item in errors
    )
    if not browser_failure:
        return 60
    depth = diagnostics.get("resume_depth")
    resume_depth = depth if isinstance(depth, int) and depth >= 1 else 0
    delay = base_seconds
    for _ in range(resume_depth):
        delay *= 2
    return min(delay, 3600)


def _retry_low_memory(
    task: Task, available_mb: int, threshold_mb: int, retry_seconds: int
) -> Never:
    logger.warning(
        "scan_deferred_low_memory",
        mem_available_mb=available_mb,
        threshold_mb=threshold_mb,
        retry_seconds=retry_seconds,
    )
    raise task.retry(
        exc=RuntimeError(
            f"crawler deferred: MemAvailable {available_mb} MiB below {threshold_mb} MiB"
        ),
        countdown=retry_seconds,
        max_retries=24,
    )


def _retry_busy(task: Task, operation: str) -> Never:
    raise task.retry(
        exc=RuntimeError(f"{operation} is already running"),
        countdown=60,
        max_retries=360,
    )


@celery_app.task(
    bind=True,
    name="job_agent.scheduler.run_scan",
    acks_late=True,
)
def run_scan_task(self: Task, scan_id: str) -> dict[str, Any]:
    parsed_scan_id = _parse_uuid(scan_id, name="scan_id")
    bind_log_context(scan_id=scan_id, correlation_id=scan_id)
    source_id, scan_type = _run_async(_scan_identity(parsed_scan_id))
    settings = get_settings()
    available_mb = _mem_available_mb()
    if available_mb is not None and available_mb < settings.crawler_min_mem_available_mb:
        _retry_low_memory(
            self,
            available_mb,
            settings.crawler_min_mem_available_mb,
            settings.crawler_memory_retry_seconds,
        )
    client = _redis_client()
    service = _scan_service()
    owner_task_id = str(self.request.id or f"inline:{scan_id}")
    try:
        key = lock_key("source-operation", str(source_id))
        with leased_redis_lock(
            client,
            key,
            ttl_seconds=settings.crawler_scan_lease_seconds,
        ) as lease:
            if lease is None:
                _retry_busy(self, f"{scan_type.value} scan for source {source_id}")
            claimed = _run_async(service.claim_scan(parsed_scan_id, owner_task_id))
            if not claimed:
                current = _run_async(_load_scan(parsed_scan_id))
                logger.info(
                    "scan_task_not_owner",
                    scan_id=scan_id,
                    source_id=str(source_id),
                    scan_type=scan_type.value,
                    owner_task_id=current.owner_task_id,
                    status=current.status.value,
                )
                return _scan_result_payload(current)
            try:
                run = _run_async(
                    _run_scan_with_timeout(
                        service,
                        parsed_scan_id,
                        owner_task_id=owner_task_id,
                        timeout_seconds=_scan_timeout_seconds(scan_type),
                    )
                )
            except BaseException as exc:
                try:
                    _run_async(
                        service.interrupt_scan(
                            parsed_scan_id,
                            reason="worker_exception",
                            expected_owner_task_id=owner_task_id,
                            details={"error_type": type(exc).__name__},
                        )
                    )
                finally:
                    raise
        if lease.lease_lost:
            logger.warning(
                "scan_lock_lease_lost",
                scan_id=scan_id,
                source_id=str(source_id),
                scan_type=scan_type.value,
            )
        _record_scan_result(run)
        health = _run_async(_source_health(source_id))
        _set_source_health_metric(source_id, health)
        processing_task_id: str | None = None
        if run.status in {
            RunStatus.SUCCEEDED,
            RunStatus.PARTIAL,
        } and _run_async(_downstream_actions_allowed(source_id)):
            processing_task = process_unprocessed_jobs_task.apply_async(queue="matching")
            processing_task_id = str(processing_task.id)

        resume_scan_id: str | None = None
        if (
            run.status == RunStatus.PARTIAL
            and health == SourceHealth.HEALTHY
            and scan_has_pending_reference_failures(run)
            and not scan_resume_is_stalled(run)
            and (
                not isinstance(run.diagnostics, dict)
                or not isinstance(run.diagnostics.get("resume_depth"), int)
                or run.diagnostics["resume_depth"] < MAX_AUTO_RESUME_DEPTH
            )
        ):
            resumed = _run_async(
                _scan_service().create_scan(
                    source_id,
                    scan_type,
                    resume_scan_id=run.id,
                    actor="partial_resume",
                )
            )
            reservation = reserve_once(
                client,
                lock_key("partial-resume", str(run.id)),
                ttl_seconds=86_400,
            )
            if reservation is not None:
                try:
                    resume_delay = _browser_partial_resume_delay_seconds(
                        run, settings.crawler_browser_resume_backoff_seconds
                    )
                    run_scan_task.apply_async(
                        args=[str(resumed.id)],
                        queue="crawling",
                        countdown=resume_delay,
                    )
                    resume_scan_id = str(resumed.id)
                    logger.info(
                        "partial_scan_resume_scheduled",
                        scan_id=str(run.id),
                        resume_scan_id=resume_scan_id,
                        source_id=str(source_id),
                        scan_type=scan_type.value,
                    )
                except Exception:
                    reservation.release()
                    raise
        return {
            "scan_id": str(run.id),
            "source_id": str(run.source_id),
            "scan_type": run.scan_type.value,
            "status": run.status.value,
            "found_jobs": run.found_jobs,
            "new_jobs": run.new_jobs,
            "updated_jobs": run.updated_jobs,
            "unchanged_jobs": run.unchanged_jobs,
            "errors": run.parsing_errors + run.network_errors,
            "processing_task_id": processing_task_id,
            "resume_scan_id": resume_scan_id,
        }
    finally:
        close_redis_client(client)


@celery_app.task(name="job_agent.scheduler.start_scan")
def start_scan_task(source_id: str, scan_type: str) -> dict[str, str]:
    parsed_source_id = _parse_uuid(source_id, name="source_id")
    try:
        parsed_scan_type = ScanType(scan_type)
    except ValueError as exc:
        raise ValueError("scan_type must be full or incremental") from exc
    if parsed_scan_type == ScanType.RECHECK:
        raise ValueError("use recheck_source task for rechecks")
    run = _run_async(
        _scan_service().create_scan(parsed_source_id, parsed_scan_type, actor="scheduler_task")
    )
    run_scan_task.apply_async(args=[str(run.id)], queue="crawling")
    return {"scan_id": str(run.id), "status": run.status.value}


@celery_app.task(bind=True, name="job_agent.scheduler.recheck_source", acks_late=True)
def recheck_source_task(self: Task, source_id: str) -> dict[str, int | str]:
    parsed_source_id = _parse_uuid(source_id, name="source_id")
    bind_log_context(source_id=source_id, correlation_id=source_id)
    client = _redis_client()
    try:
        with leased_redis_lock(
            client,
            lock_key("source-operation", source_id),
            ttl_seconds=900,
        ) as lease:
            if lease is None:
                _retry_busy(self, f"recheck for source {source_id}")
            policy = _run_async(_recheck_policy(parsed_source_id))
            result = _run_async(
                _scan_service().recheck_active_jobs(
                    parsed_source_id,
                    close_after_confirmed_absence_count=(
                        policy.close_after_confirmed_absence_count
                    ),
                    max_jobs_per_run=policy.max_jobs_per_run,
                    min_recheck_interval_hours=policy.min_recheck_interval_hours,
                )
            )
        if lease.lease_lost:
            logger.warning("recheck_lock_lease_lost", source_id=source_id)
        health = _run_async(_source_health(parsed_source_id))
        payload: dict[str, int | str] = {
            **result,
            "source_id": source_id,
            "health": health.value if health is not None else "unknown",
        }
        if result.get("guardrail_triggered"):
            logger.warning(
                "recheck_source_guardrail_triggered",
                source_id=source_id,
                health=payload["health"],
                checked=result.get("checked", 0),
                absent=result.get("absent", 0),
                sentinel_checked=result.get("sentinel_checked", 0),
                sentinel_absent=result.get("sentinel_absent", 0),
                adapter_errors=result.get("errors", 0),
            )
        _set_source_health_metric(parsed_source_id, health)
        return payload
    finally:
        close_redis_client(client)


def _resume_orphaned_scan(client: Redis, orphan: ScanRun) -> str | None:
    reservation = reserve_once(
        client,
        lock_key("orphan-resume", str(orphan.id)),
        ttl_seconds=86_400,
    )
    if reservation is None:
        return None
    try:
        resumed = _run_async(
            _scan_service().create_scan(
                orphan.source_id,
                orphan.scan_type,
                resume_scan_id=orphan.id,
                actor="orphan_reconciliation",
            )
        )
        if resumed.status == RunStatus.RUNNING:
            return None
        run_scan_task.apply_async(args=[str(resumed.id)], queue="crawling")
        logger.info(
            "orphan_scan_resume_scheduled",
            orphan_scan_id=str(orphan.id),
            resume_scan_id=str(resumed.id),
            source_id=str(orphan.source_id),
            scan_type=orphan.scan_type.value,
        )
        return str(resumed.id)
    except Exception:
        reservation.release()
        raise


@celery_app.task(name="job_agent.scheduler.reconcile_orphaned_scans")
def reconcile_orphaned_scans_task() -> dict[str, Any]:
    client = _redis_client()
    try:
        reconciled = _run_async(_reconcile_orphaned_scans(client))
        resumed = [
            resume_id
            for orphan in reconciled
            if (resume_id := _resume_orphaned_scan(client, orphan)) is not None
        ]
        return {
            "reconciled": [str(run.id) for run in reconciled],
            "resumed": resumed,
            "count": len(reconciled),
        }
    finally:
        close_redis_client(client)


def _operation_allowed_for_source(source: SourceSchedule, operation: str) -> bool:
    # A degraded source may run its scheduled incremental scan as a bounded recovery probe.
    # Rechecks and full scans stay suppressed until a successful scan restores HEALTHY.
    return source.health_status != SourceHealth.DEGRADED or operation == "incremental"


def _degraded_recovery_probe_due(source: SourceSchedule, operation: str, now: datetime) -> bool:
    if (
        source.adapter_type != "rabota_md"
        or source.health_status != SourceHealth.DEGRADED
        or operation != "incremental"
    ):
        return True
    return now.astimezone(UTC).hour % _RABOTA_DEGRADED_RECOVERY_INTERVAL_HOURS == 0


def _dispatch_one(
    client: Redis,
    source: SourceSchedule,
    operation: str,
    now: datetime,
) -> str | None:
    if not _operation_allowed_for_source(source, operation):
        return None
    # A newly enabled source must complete its initial full scan before Beat can launch
    # incremental/recheck work. This keeps the operator-controlled first scan deterministic.
    if operation in {"incremental", "recheck"} and not source.has_successful_full_scan:
        return None
    expression = _configured_schedule(source, operation)
    if not cron_expression_is_due(expression, now):
        return None
    if not _degraded_recovery_probe_due(source, operation, now):
        return None
    minute_slot = now.astimezone(UTC).strftime("%Y%m%d%H%M")
    reservation = reserve_once(
        client,
        lock_key("beat", str(source.source_id), operation, minute_slot),
        ttl_seconds=172_800,
    )
    if reservation is None:
        return None
    try:
        if operation == "recheck":
            recheck_source_task.apply_async(args=[str(source.source_id)], queue="crawling")
            return f"recheck:{source.source_id}"
        scan_type = ScanType(operation)
        reconciled = _run_async(
            _reconcile_orphaned_scans(
                client,
                source_id=source.source_id,
                scan_type=scan_type,
                limit=1,
                now=now,
            )
        )
        if reconciled:
            resume_id = _resume_orphaned_scan(client, reconciled[0])
            if resume_id is not None:
                return f"{operation}:{resume_id}"

        run, should_enqueue = _run_async(
            _get_or_create_queued_scan(
                source.source_id,
                scan_type,
                resume_from_checkpoint=_resume_from_checkpoint_enabled(source, operation),
            )
        )
        if not should_enqueue:
            logger.info(
                "scheduled_scan_already_running",
                scan_id=str(run.id),
                source_id=str(source.source_id),
                scan_type=scan_type.value,
            )
            return None
        run_scan_task.apply_async(args=[str(run.id)], queue="crawling")
        return f"{operation}:{run.id}"
    except Exception:
        reservation.release()
        raise


@celery_app.task(name="job_agent.scheduler.dispatch_due_sources")
def dispatch_due_sources_task() -> dict[str, Any]:
    now = datetime.now(UTC)
    sources = _run_async(_load_enabled_sources())
    client = _redis_client()
    dispatched: list[str] = []
    invalid_schedules: list[dict[str, str]] = []
    dispatch_errors: list[dict[str, str]] = []
    try:
        for source in sources:
            for operation in ("incremental", "recheck", "full"):
                try:
                    result = _dispatch_one(client, source, operation, now)
                except ValueError as exc:
                    invalid_schedules.append(
                        {
                            "source_id": str(source.source_id),
                            "operation": operation,
                            "error_type": type(exc).__name__,
                        }
                    )
                    logger.error(
                        "invalid_source_schedule",
                        source_id=str(source.source_id),
                        operation=operation,
                        error_type=type(exc).__name__,
                    )
                    continue
                except Exception as exc:
                    dispatch_errors.append(
                        {
                            "source_id": str(source.source_id),
                            "operation": operation,
                            "error_type": type(exc).__name__,
                        }
                    )
                    logger.error(
                        "source_dispatch_failed",
                        source_id=str(source.source_id),
                        operation=operation,
                        error_type=type(exc).__name__,
                    )
                    continue
                if result is not None:
                    dispatched.append(result)
    finally:
        close_redis_client(client)
    return {
        "checked_sources": len(sources),
        "dispatched": dispatched,
        "invalid_schedules": invalid_schedules,
        "dispatch_errors": dispatch_errors,
        "slot": now.strftime("%Y-%m-%dT%H:%MZ"),
    }


def _run_locked_periodic[ResultT](
    operation: str,
    awaitable: Coroutine[Any, Any, ResultT],
    *,
    ttl_seconds: int,
) -> ResultT | dict[str, str]:
    client = _redis_client()
    try:
        with leased_redis_lock(
            client,
            lock_key("periodic", operation),
            ttl_seconds=ttl_seconds,
        ) as lease:
            if lease is None:
                awaitable.close()
                return {"status": "already_running", "operation": operation}
            result = _run_async(awaitable)
        if lease.lease_lost:
            logger.warning("periodic_lock_lease_lost", operation=operation)
        return result
    finally:
        close_redis_client(client)


@celery_app.task(name="job_agent.scheduler.process_unprocessed_jobs")
def process_unprocessed_jobs_task() -> int | dict[str, str]:
    from app.matching.service import process_unprocessed_jobs

    result = _run_locked_periodic("matching", process_unprocessed_jobs(), ttl_seconds=900)
    if isinstance(result, int) and result > 0:
        prepare_pending_applications_task.apply_async(queue="applications")
    return result


@celery_app.task(name="job_agent.scheduler.prepare_pending_applications")
def prepare_pending_applications_task() -> int | dict[str, str]:
    from app.applications.service import prepare_pending_applications

    return _run_locked_periodic(
        "applications-state-mutation",
        prepare_pending_applications(),
        ttl_seconds=900,
    )


@celery_app.task(name="job_agent.scheduler.reconcile_auto_approved_applications")
def reconcile_auto_approved_applications_task() -> dict[str, int] | dict[str, str]:
    from app.email.service import reconcile_auto_approved_application_states

    return _run_locked_periodic(
        "applications-state-mutation",
        reconcile_auto_approved_application_states(),
        ttl_seconds=900,
    )


@celery_app.task(name="job_agent.scheduler.refresh_dirty_deferred_applications")
def refresh_dirty_deferred_applications_task() -> dict[str, int] | dict[str, str]:
    from app.applications.service import refresh_dirty_deferred_applications

    return _run_locked_periodic(
        "deferred-policy-refresh",
        refresh_dirty_deferred_applications(),
        ttl_seconds=120,
    )


@celery_app.task(name="job_agent.scheduler.send_auto_approved_applications")
def send_auto_approved_applications_task() -> int | dict[str, str]:
    from app.email.service import send_auto_approved_applications

    return _run_locked_periodic(
        "send-auto-approved",
        send_auto_approved_applications(),
        ttl_seconds=900,
    )


@celery_app.task(name="job_agent.scheduler.retry_temporary_failures")
def retry_temporary_failures_task() -> int | dict[str, str]:
    from app.email.service import retry_temporary_failures

    return _run_locked_periodic(
        "retry-temporary-email",
        retry_temporary_failures(),
        ttl_seconds=900,
    )


@celery_app.task(name="job_agent.scheduler.reconcile_email_delivery_status")
def reconcile_email_delivery_status_task() -> dict[str, int | str] | dict[str, str]:
    from app.email.delivery import EmailDeliveryReconciliationService

    return _run_locked_periodic(
        "reconcile-email-delivery",
        EmailDeliveryReconciliationService(get_settings(), async_session_factory).reconcile(),
        ttl_seconds=600,
    )


@celery_app.task(name="job_agent.scheduler.generate_daily_report")
def generate_daily_report_task() -> dict[str, Any]:
    from app.reports.service import generate_daily_report

    result = _run_locked_periodic("daily-report", generate_daily_report(), ttl_seconds=900)
    return result


@celery_app.task(name="job_agent.scheduler.train_learning_models")
def train_learning_models_task() -> int | dict[str, str]:
    from app.learning.training import train_all_profiles

    return _run_locked_periodic("train-learning-models", train_all_profiles(), ttl_seconds=1800)


@celery_app.task(name="job_agent.scheduler.record_learning_shadow")
def record_learning_shadow_task() -> int | dict[str, str]:
    from app.learning.shadow import record_learning_shadow

    return _run_locked_periodic("record-learning-shadow", record_learning_shadow(), ttl_seconds=600)


@celery_app.task(name="job_agent.scheduler.finalize_pending_calls")
def finalize_pending_calls_task() -> dict[str, Any]:
    from app.phone.summary import finalize_pending_calls

    return _run_locked_periodic("phone-finalize", finalize_pending_calls(), ttl_seconds=600)


@celery_app.task(name="job_agent.scheduler.deliver_phone_notifications")
def deliver_phone_notifications_task() -> dict[str, Any]:
    from app.phone.telegram import deliver_pending_phone_notifications

    settings = get_settings()
    lease = settings.phone_telegram_lease_seconds
    batch = settings.phone_telegram_batch
    return _run_locked_periodic(
        "phone-telegram",
        deliver_pending_phone_notifications(),
        ttl_seconds=max(5, lease, batch * 15),
    )


@celery_app.task(name="job_agent.scheduler.prune_phone_evidence")
def prune_phone_evidence_task() -> dict[str, Any]:
    from app.phone.evidence import prune_phone_evidence

    return _run_locked_periodic("phone-evidence-prune", prune_phone_evidence(), ttl_seconds=900)


@celery_app.task(name="job_agent.scheduler.ingest_phonegate_sms")
def ingest_phonegate_sms_task() -> dict[str, int] | dict[str, str]:
    from app.phone.sms import ingest_phonegate_sms

    interval = get_settings().phone_sms_poll_interval_seconds
    ttl_seconds = max(5, int(interval * 2))
    return _run_locked_periodic("phone-sms", ingest_phonegate_sms(), ttl_seconds=ttl_seconds)


@celery_app.task(name="job_agent.scheduler.reconcile_phone_sms")
def reconcile_phone_sms_task() -> dict[str, int] | dict[str, str]:
    from app.phone.sms import reconcile_pending_sms

    interval = get_settings().phone_sms_poll_interval_seconds
    ttl_seconds = max(5, int(interval * 2))
    return _run_locked_periodic(
        "phone-sms-reconcile",
        reconcile_pending_sms(session_factory=async_session_factory),
        ttl_seconds=ttl_seconds,
    )


__all__ = [
    "DEFAULT_SOURCE_SCHEDULES",
    "close_task_event_loop",
    "cron_expression_is_due",
    "deliver_phone_notifications_task",
    "dispatch_due_sources_task",
    "finalize_pending_calls_task",
    "generate_daily_report_task",
    "ingest_phonegate_sms_task",
    "prepare_pending_applications_task",
    "process_unprocessed_jobs_task",
    "prune_phone_evidence_task",
    "rabota_md_proxy_reserve_maintenance_task",
    "rabota_md_waf_canary_task",
    "recheck_source_task",
    "reconcile_auto_approved_applications_task",
    "reconcile_email_delivery_status_task",
    "reconcile_phone_sms_task",
    "record_learning_shadow_task",
    "refresh_dirty_deferred_applications_task",
    "retry_temporary_failures_task",
    "run_scan_task",
    "send_auto_approved_applications_task",
    "start_scan_task",
    "train_learning_models_task",
]


@celery_app.task(name="job_agent.scheduler.rabota_md_proxy_reserve_maintenance")
def rabota_md_proxy_reserve_maintenance_task() -> dict[str, object]:
    client = _redis_client()
    try:
        with leased_redis_lock(
            client,
            lock_key("rabota_md", "proxy_reserve_maintenance"),
            ttl_seconds=240,
        ) as lease:
            if lease is None:
                return {"outcome": "skipped", "reason": "lease_busy"}
            return _run_async(_rabota_md_proxy_reserve_maintenance())
    finally:
        close_redis_client(client)


async def _record_rabota_proxy_reserve_health(
    *,
    redis_url: str | None,
    ready: int,
) -> tuple[int, str | None]:
    if not redis_url:
        return 0, None

    from redis.asyncio import Redis as AsyncRedis

    key = "crawler:rabota_md:proxy_pool:maintenance_health"
    redis = AsyncRedis.from_url(redis_url, decode_responses=True)
    try:
        now = datetime.now(UTC).isoformat()
        if ready > 0:
            empty_cycles = 0
            await redis.hset(
                key,
                mapping={
                    "consecutive_empty_cycles": "0",
                    "last_ready_at": now,
                    "last_ready_count": str(ready),
                },
            )
        else:
            empty_cycles = int(await redis.hincrby(key, "consecutive_empty_cycles", 1))
            await redis.hset(key, mapping={"last_ready_count": "0"})
        await redis.expire(key, 7 * 24 * 3600)
        last_ready_at = await redis.hget(key, "last_ready_at")
        return empty_cycles, last_ready_at
    finally:
        await redis.aclose()


async def _rabota_md_proxy_reserve_maintenance() -> dict[str, object]:
    from app.crawlers.adapters.rabota_md.transport import (
        effective_waf_user_agent,
        warm_rabota_proxy_reserve,
    )

    source = await _rabota_md_waf_canary_source()
    settings = get_settings()
    if (
        source is None
        or not _rabota_md_uses_waf_http(source)
        or not settings.rabota_proxy_pool_enabled
        or not settings.rabota_proxy_free_fallback_enabled
    ):
        return {"outcome": "skipped"}

    configured = source.configuration.get("source", source.configuration)
    raw = configured if isinstance(configured, dict) else {}
    configured_ua = raw.get("user_agent")
    user_agent = effective_waf_user_agent(
        configured_ua
        if isinstance(configured_ua, str) and configured_ua.strip()
        else settings.crawler_user_agent
    )
    counts = await warm_rabota_proxy_reserve(
        base_url=source.base_url.rstrip("/"),
        user_agent=user_agent,
        requests_per_minute=int(raw.get("requests_per_minute", min(source.rate_limit, 60))),
        minimum_interval_seconds=float(raw.get("minimum_interval_seconds", 1.2)),
        timeout_seconds=float(raw.get("timeout_seconds", 30.0)),
        max_redirects=int(raw.get("max_redirects", 3)),
        fallback_transport=str(raw.get("fallback_transport", "stealth_browser")),
        browser_max_navigations_per_page=int(raw.get("browser_max_navigations_per_page", 50)),
        max_preflight_attempts=min(
            int(getattr(settings, "rabota_proxy_max_preflight_attempts", 12)),
            int(getattr(settings, "rabota_proxy_maintenance_max_preflight_attempts", 4)),
        ),
    )
    primary_probe = await _rabota_md_primary_egress_probe(
        source,
        user_agent=user_agent,
    )
    probe_outcome = str(primary_probe["outcome"])
    if probe_outcome in {"success", "failure"}:
        RABOTA_PROXY_PRIMARY_REACHABLE.set(1 if probe_outcome == "success" else 0)
    if probe_outcome == "failure":
        logger.warning(
            "rabota_md_proxy_primary_probe_failed",
            error_type=primary_probe.get("error_type"),
            ready=counts["ready"],
            candidates=counts["candidates"],
        )
    target_ready = settings.rabota_proxy_target_ready_free
    empty_cycles, last_ready_at = await _record_rabota_proxy_reserve_health(
        redis_url=getattr(settings, "redis_url", None),
        ready=counts["ready"],
    )
    if counts["ready"] >= target_ready:
        outcome = "ready"
        logger.info(
            "rabota_md_proxy_reserve_ready",
            ready=counts["ready"],
            target_ready=target_ready,
            candidates=counts["candidates"],
            primary_probe=probe_outcome,
            consecutive_empty_cycles=empty_cycles,
            last_ready_at=last_ready_at,
        )
    elif counts["ready"] > 0:
        outcome = "degraded"
        logger.warning(
            "rabota_md_proxy_reserve_degraded",
            ready=counts["ready"],
            target_ready=target_ready,
            candidates=counts["candidates"],
            primary_probe=probe_outcome,
            consecutive_empty_cycles=empty_cycles,
            last_ready_at=last_ready_at,
        )
    else:
        outcome = "empty"
        logger.warning(
            "rabota_md_proxy_reserve_empty",
            ready=0,
            target_ready=target_ready,
            candidates=counts["candidates"],
            primary_probe=probe_outcome,
            consecutive_empty_cycles=empty_cycles,
            last_ready_at=last_ready_at,
        )
    return {
        "outcome": outcome,
        "ready": counts["ready"],
        "target_ready": target_ready,
        "candidates": counts["candidates"],
        "primary_probe": probe_outcome,
        "consecutive_empty_cycles": empty_cycles,
        "last_ready_at": last_ready_at,
    }


@celery_app.task(name="job_agent.scheduler.rabota_md_waf_canary")
def rabota_md_waf_canary_task() -> dict[str, str]:
    """Daily canary: prove that the live AWS WAF protocol is still compatible.

    ``challenge.js`` bytes are dynamic, so their SHA-256 is observability only.
    A successful live token solve writes a short-lived Redis compatibility marker;
    normal crawls use the pure solver only while that marker is fresh.
    """
    return _run_async(_rabota_md_waf_canary())


async def _rabota_md_waf_canary_source() -> JobSource | None:
    async with async_session_factory() as session:
        source = await session.scalar(
            select(JobSource)
            .where(
                JobSource.adapter_type == "rabota_md",
                JobSource.enabled.is_(True),
            )
            .limit(1)
        )
        return source


async def _rabota_md_primary_egress_probe(
    source: JobSource, *, user_agent: str
) -> dict[str, object]:
    import httpx

    settings = get_settings()
    if settings.rabota_proxy_primary_url is None:
        return {"outcome": "skipped"}
    primary_url = settings.rabota_proxy_primary_url.get_secret_value()
    try:
        async with httpx.AsyncClient(
            proxy=primary_url,
            timeout=httpx.Timeout(10.0, connect=5.0),
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": user_agent},
        ) as client:
            response = await client.get(f"{source.base_url.rstrip('/')}/ru/")
    except httpx.TransportError as exc:
        return {"outcome": "failure", "error_type": type(exc).__name__}
    return {"outcome": "success", "http_status": response.status_code}


def _rabota_md_uses_waf_http(source: JobSource) -> bool:
    configured = source.configuration.get("source", source.configuration)
    raw = configured if isinstance(configured, dict) else {}
    transport = raw.get("transport")
    if transport is None:
        transport = "stealth_browser" if raw.get("use_stealth_browser", True) else "waf_http"
    return transport == "waf_http"


async def _rabota_md_waf_pagination_probe(
    source: JobSource, token: str, *, user_agent: str
) -> None:
    """Probe one real pagination request through the configured WAF+browser stack.

    Token solving alone is not enough: AWS WAF can accept the token for document
    navigation while escalating the AJAX pagination POST to CAPTCHA.
    """

    from redis.asyncio import Redis as AsyncRedis

    from app.crawlers.adapters.rabota_md.fallback import FallbackFetcher
    from app.crawlers.adapters.rabota_md.waf.http_client import WafHttpClient
    from app.crawlers.adapters.rabota_md.waf.token_provider import (
        MintedWafToken,
        WafTokenProvider,
    )
    from app.crawlers.browser import AWS_WAF_BROWSER_ALLOWED_DOMAINS, StealthPlaywrightBrowser
    from app.crawlers.http import AsyncRateLimiter, SecureHttpClient

    configured = source.configuration.get("source", source.configuration)
    raw = configured if isinstance(configured, dict) else {}
    incremental = raw.get("incremental_scan")
    incremental = incremental if isinstance(incremental, dict) else {}
    slugs = incremental.get("category_slugs")
    slugs = [slug for slug in slugs if isinstance(slug, str)] if isinstance(slugs, list) else []
    slug = "operating" if "operating" in slugs else (slugs[0] if slugs else "others")
    locales = raw.get("locale_priority")
    locales = (
        [locale for locale in locales if isinstance(locale, str)]
        if isinstance(locales, list)
        else []
    )
    locale = locales[0] if locales else "ru"
    base_url = source.base_url.rstrip("/")
    referer = f"{base_url}/{locale}/vacancies/category/{slug}"
    page_url = f"{referer}/2"

    requests_per_minute = int(raw.get("requests_per_minute", min(source.rate_limit, 60)))
    minimum_interval_seconds = float(raw.get("minimum_interval_seconds", 1.2))
    timeout_seconds = float(raw.get("timeout_seconds", 30.0))
    max_redirects = int(raw.get("max_redirects", 3))
    browser_max_navigations = int(raw.get("browser_max_navigations_per_page", 50))
    limiter = AsyncRateLimiter(
        requests_per_minute, minimum_interval_seconds=minimum_interval_seconds
    )

    class _CanaryTokenBackend:
        async def mint(self) -> MintedWafToken:
            return MintedWafToken(token)

    redis = AsyncRedis.from_url(get_settings().redis_url)
    provider = WafTokenProvider(
        redis,
        [_CanaryTokenBackend()],
        token_key="crawler:rabota_md:waf_canary_probe_token",  # noqa: S106 - Redis key
        lock_key="crawler:rabota_md:waf_canary_probe_token:refresh_lock",
        max_ttl_seconds=300,
        safety_margin_seconds=0,
    )
    await provider.publish_token(MintedWafToken(token))
    secure = SecureHttpClient(
        allowed_domains=("rabota.md", "www.rabota.md"),
        user_agent=user_agent,
        requests_per_minute=requests_per_minute,
        minimum_interval_seconds=minimum_interval_seconds,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        rate_limiter=limiter,
    )
    primary = WafHttpClient(secure, provider)
    browser = StealthPlaywrightBrowser(
        allowed_domains=AWS_WAF_BROWSER_ALLOWED_DOMAINS,
        requests_per_minute=requests_per_minute,
        minimum_interval_seconds=minimum_interval_seconds,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
        max_navigations_per_page=browser_max_navigations,
    )
    fetcher = FallbackFetcher(primary, browser, provider, max_switches_per_scan=1)
    try:
        response = await fetcher.post_html_fragment(page_url, referer=referer)
        if response.status_code != 200:
            raise RuntimeError(f"pagination probe returned HTTP {response.status_code}")
    finally:
        await fetcher.aclose()


async def _rabota_md_proxy_pool_probe(source: JobSource, *, user_agent: str) -> None:
    """Probe the same GET + AJAX POST path used by scans through the configured egress pool."""
    from app.crawlers.adapters.rabota_md.transport import build_waf_fetcher

    configured = source.configuration.get("source", source.configuration)
    raw = configured if isinstance(configured, dict) else {}
    incremental = raw.get("incremental_scan")
    incremental = incremental if isinstance(incremental, dict) else {}
    slugs = incremental.get("category_slugs")
    slugs = [slug for slug in slugs if isinstance(slug, str)] if isinstance(slugs, list) else []
    slug = "operating" if "operating" in slugs else (slugs[0] if slugs else "others")
    locales = raw.get("locale_priority")
    locales = (
        [item for item in locales if isinstance(item, str)] if isinstance(locales, list) else []
    )
    locale = locales[0] if locales else "ru"
    base_url = source.base_url.rstrip("/")
    referer = f"{base_url}/{locale}/vacancies/category/{slug}"
    page_url = f"{referer}/2"

    fetcher = build_waf_fetcher(
        base_url=base_url,
        user_agent=user_agent,
        requests_per_minute=int(raw.get("requests_per_minute", min(source.rate_limit, 60))),
        minimum_interval_seconds=float(raw.get("minimum_interval_seconds", 1.2)),
        timeout_seconds=float(raw.get("timeout_seconds", 30.0)),
        max_redirects=int(raw.get("max_redirects", 3)),
        fallback_transport=str(raw.get("fallback_transport", "stealth_browser")),
        browser_max_navigations_per_page=int(raw.get("browser_max_navigations_per_page", 50)),
    )
    try:
        landing = await fetcher.get(f"{base_url}/{locale}/")
        if landing.status_code != 200:
            raise RuntimeError(f"proxy landing probe returned HTTP {landing.status_code}")
        pagination = await fetcher.post_html_fragment(page_url, referer=referer)
        if pagination.status_code != 200:
            raise RuntimeError(f"proxy pagination probe returned HTTP {pagination.status_code}")
    finally:
        await fetcher.aclose()


async def _rabota_md_waf_canary() -> dict[str, str]:
    from redis.asyncio import Redis as AsyncRedis

    from app.crawlers.adapters.rabota_md.transport import effective_waf_user_agent
    from app.crawlers.adapters.rabota_md.waf.solver import AwsWafSolver
    from app.crawlers.adapters.rabota_md.waf.watchdog import ScriptWatchdog
    from app.observability.metrics import WAF_SOLVER_CANARY, WAF_SOLVER_COMPATIBILITY

    source = await _rabota_md_waf_canary_source()
    if source is None or not _rabota_md_uses_waf_http(source):
        WAF_SOLVER_CANARY.labels(outcome="skipped").inc()
        return {"outcome": "skipped", "reason": "waf_http_not_enabled"}

    configured = source.configuration.get("source", source.configuration)
    raw_config = configured if isinstance(configured, dict) else {}
    configured_ua = raw_config.get("user_agent")
    user_agent = effective_waf_user_agent(
        configured_ua
        if isinstance(configured_ua, str) and configured_ua.strip()
        else get_settings().crawler_user_agent
    )

    settings = get_settings()
    if settings.rabota_proxy_pool_enabled:
        from app.crawlers.adapters.rabota_md.proxy_pool import (
            ProxyEndpoint,
            RabotaProxyPool,
        )
        from app.crawlers.adapters.rabota_md.transport import _prove_free_waf_candidate
        from app.crawlers.adapters.rabota_md.waf.errors import (
            WafSolveFailed,
            WafSolverCompatibilityError,
            WafTransportError,
            WafUnsupportedChallenge,
        )

        primary_outcome = "success"
        primary_error: str | None = None
        try:
            await _rabota_md_proxy_pool_probe(source, user_agent=user_agent)
        except Exception as exc:
            primary_outcome = "failure"
            primary_error = type(exc).__name__
            logger.warning(
                "rabota_md_proxy_pool_canary_failed",
                error_type=primary_error,
            )
        else:
            logger.info("rabota_md_proxy_pool_canary_ok")

        primary = (
            settings.rabota_proxy_primary_url.get_secret_value()
            if settings.rabota_proxy_primary_url is not None
            else None
        )
        pool = RabotaProxyPool(
            AsyncRedis.from_url(settings.redis_url),
            primary_url=primary,
            target_url=f"{source.base_url.rstrip('/')}/ru/",
            user_agent=user_agent,
            free_fallback_enabled=settings.rabota_proxy_free_fallback_enabled,
            discovery_batch=settings.rabota_proxy_discovery_batch,
            validation_concurrency=settings.rabota_proxy_validation_concurrency,
            validation_timeout_seconds=settings.rabota_proxy_validation_timeout_seconds,
            candidate_ttl_seconds=settings.rabota_proxy_candidate_ttl_seconds,
            ready_ttl_seconds=settings.rabota_proxy_ready_ttl_seconds,
            revalidation_grace_seconds=settings.rabota_proxy_revalidation_grace_seconds,
            revalidation_retry_seconds=settings.rabota_proxy_revalidation_retry_seconds,
            revalidation_max_failures=settings.rabota_proxy_revalidation_max_failures,
            min_fresh_free=settings.rabota_proxy_min_fresh_free,
            ban_cooldown_seconds=settings.rabota_proxy_ban_cooldown_seconds,
            dead_cooldown_seconds=settings.rabota_proxy_dead_cooldown_seconds,
            primary_dead_cooldown_seconds=settings.rabota_proxy_primary_dead_cooldown_seconds,
        )
        solver_outcome = "skipped_no_candidate"
        solver_error: str | None = None
        try:
            candidates = await pool.promotion_endpoints(
                set(),
                limit=settings.rabota_proxy_maintenance_max_preflight_attempts,
            )
            candidate = next(
                (
                    endpoint
                    for endpoint in candidates
                    if endpoint.kind == "free" and endpoint.capability == "waf_candidate"
                ),
                None,
            )
            if candidate is not None:
                try:
                    await _prove_free_waf_candidate(
                        endpoint=candidate,
                        base_url=source.base_url.rstrip("/"),
                        waf_user_agent=user_agent,
                        requests_per_minute=int(
                            raw_config.get("requests_per_minute", min(source.rate_limit, 60))
                        ),
                        minimum_interval_seconds=float(
                            raw_config.get("minimum_interval_seconds", 1.2)
                        ),
                        timeout_seconds=float(raw_config.get("timeout_seconds", 30.0)),
                        max_redirects=int(raw_config.get("max_redirects", 3)),
                        resolver=None,
                        force_proof=True,
                    )
                except WafTransportError as exc:
                    solver_outcome = "inconclusive_transport"
                    solver_error = exc.error_type
                    logger.warning(
                        "rabota_md_pure_solver_canary_inconclusive",
                        error_type=exc.error_type,
                        stage=exc.stage,
                    )
                except (
                    WafUnsupportedChallenge,
                    WafSolverCompatibilityError,
                    WafSolveFailed,
                ) as exc:
                    solver_outcome = "failure"
                    solver_error = type(exc).__name__
                    redis = AsyncRedis.from_url(settings.redis_url)
                    try:
                        await ScriptWatchdog(redis).invalidate_compatibility()
                    finally:
                        await redis.aclose()
                    WAF_SOLVER_COMPATIBILITY.set(0)
                    logger.warning(
                        "rabota_md_pure_solver_canary_failed",
                        error_type=solver_error,
                    )
                else:
                    solver_outcome = "success"
                    await pool.report_success(
                        ProxyEndpoint(
                            name=candidate.name,
                            url=candidate.url,
                            kind=candidate.kind,
                            capability="full_waf",
                        ),
                        200,
                        validated_capability="full_waf",
                    )
                    WAF_SOLVER_COMPATIBILITY.set(1)
                    logger.info("rabota_md_pure_solver_canary_ok")
        finally:
            await pool.aclose()

        overall = (
            "failure"
            if primary_outcome == "failure"
            else "degraded"
            if solver_outcome == "failure"
            else "success"
        )
        WAF_SOLVER_CANARY.labels(outcome=overall).inc()
        result = {
            "outcome": overall,
            "mode": "proxy_pool",
            "primary": primary_outcome,
            "pure_solver": solver_outcome,
        }
        if primary_error:
            result["primary_error"] = primary_error
        if solver_error:
            result["solver_error"] = solver_error
        return result

    redis = AsyncRedis.from_url(settings.redis_url)
    try:
        watchdog = ScriptWatchdog(redis)
        solver = AwsWafSolver(script_hash_checker=lambda _digest: True)
        try:
            token = await solver.solve(source.base_url, user_agent)
        except Exception as exc:
            WAF_SOLVER_CANARY.labels(outcome="failure").inc()
            WAF_SOLVER_COMPATIBILITY.set(0)
            logger.warning("rabota_md_waf_canary_failed", error_type=type(exc).__name__)
            return {"outcome": "failure", "error_type": type(exc).__name__}
        script_hash = solver.last_script_hash
        if not script_hash:
            WAF_SOLVER_CANARY.labels(outcome="failure").inc()
            WAF_SOLVER_COMPATIBILITY.set(0)
            logger.warning("rabota_md_waf_canary_failed", error_type="MissingScriptHash")
            return {"outcome": "failure", "error_type": "MissingScriptHash"}
        try:
            await _rabota_md_waf_pagination_probe(source, token, user_agent=user_agent)
        except Exception as exc:
            WAF_SOLVER_CANARY.labels(outcome="failure").inc()
            WAF_SOLVER_COMPATIBILITY.set(0)
            logger.warning(
                "rabota_md_waf_canary_pagination_failed",
                error_type=type(exc).__name__,
            )
            return {
                "outcome": "failure",
                "stage": "pagination_probe",
                "error_type": type(exc).__name__,
            }
        await watchdog.record_canary_success(script_hash)
        WAF_SOLVER_CANARY.labels(outcome="success").inc()
        WAF_SOLVER_COMPATIBILITY.set(1)
        logger.info("rabota_md_waf_canary_ok", script_hash=script_hash)
        return {
            "outcome": "success",
            "script_hash": script_hash,
            "token_length": str(len(token)),
        }
    finally:
        await redis.aclose()
