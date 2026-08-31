# mypy: disable-error-code="untyped-decorator"
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Never, cast
from uuid import UUID

import structlog
from celery import Task
from celery.schedules import crontab
from redis import Redis
from sqlalchemy import select

from app.crawlers.pipeline import ScanService, scan_has_pending_reference_failures
from app.crawlers.registry import build_default_registry
from app.database import async_session_factory
from app.models.entities import JobSource, ScanRun
from app.models.enums import RunStatus, ScanType, SourceHealth
from app.observability import bind_log_context
from app.observability.metrics import SCAN_ERRORS, SCAN_JOBS, SCAN_RUNS, SOURCE_HEALTH
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


@dataclass(frozen=True, slots=True)
class SourceSchedule:
    source_id: UUID
    adapter_type: str
    configuration: dict[str, Any]
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
                            [SourceHealth.DEGRADED, SourceHealth.PAUSED, SourceHealth.DISABLED]
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
            has_successful_full_scan=source.id in successful_full_source_ids,
        )
        for source in sources
    ]


async def _get_or_create_queued_scan(source_id: UUID, scan_type: ScanType) -> ScanRun:
    async with async_session_factory() as session:
        queued = await session.scalar(
            select(ScanRun).where(
                ScanRun.source_id == source_id,
                ScanRun.scan_type == scan_type,
                ScanRun.status == RunStatus.QUEUED,
            )
        )
    if queued is not None:
        return queued
    return await _scan_service().create_scan(source_id, scan_type, actor="celery_beat")


async def _scan_identity(scan_id: UUID) -> tuple[UUID, ScanType]:
    async with async_session_factory() as session:
        run = await session.get(ScanRun, scan_id)
        if run is None:
            raise LookupError(f"scan {scan_id} does not exist")
        return run.source_id, run.scan_type


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


async def _recheck_absence_threshold(source_id: UUID) -> int:
    async with async_session_factory() as session:
        configuration = await session.scalar(
            select(JobSource.configuration).where(JobSource.id == source_id)
        )
    raw = configuration if isinstance(configuration, dict) else {}
    nested = raw.get("source")
    roots = [raw, nested] if isinstance(nested, dict) else [raw]
    for root in roots:
        section = root.get("active_job_recheck")
        if isinstance(section, dict):
            value = section.get("close_after_confirmed_absence_count")
            if isinstance(value, int) and 1 <= value <= 100:
                return value
    return 3


def _set_source_health_metric(source_id: UUID, current: SourceHealth | None) -> None:
    if current is None:
        return
    for state in _SOURCE_STATES:
        SOURCE_HEALTH.labels(str(source_id), state).set(1 if state == current.value else 0)


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
    client = _redis_client()
    try:
        key = lock_key("source-operation", str(source_id))
        with leased_redis_lock(client, key, ttl_seconds=900) as lease:
            if lease is None:
                _retry_busy(self, f"{scan_type.value} scan for source {source_id}")
            run = _run_async(_scan_service().run_scan(parsed_scan_id))
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
        ):
            resumed = _run_async(
                _scan_service().create_scan(source_id, scan_type, actor="partial_resume")
            )
            reservation = reserve_once(
                client,
                lock_key("partial-resume", str(run.id)),
                ttl_seconds=86_400,
            )
            if reservation is not None:
                try:
                    run_scan_task.apply_async(
                        args=[str(resumed.id)],
                        queue="crawling",
                        countdown=60,
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
            threshold = _run_async(_recheck_absence_threshold(parsed_source_id))
            result = _run_async(
                _scan_service().recheck_active_jobs(
                    parsed_source_id,
                    close_after_confirmed_absence_count=threshold,
                )
            )
        if lease.lease_lost:
            logger.warning("recheck_lock_lease_lost", source_id=source_id)
        payload: dict[str, int | str] = {**result, "source_id": source_id}
        health = _run_async(_source_health(parsed_source_id))
        _set_source_health_metric(parsed_source_id, health)
        return payload
    finally:
        close_redis_client(client)


def _dispatch_one(
    client: Redis,
    source: SourceSchedule,
    operation: str,
    now: datetime,
) -> str | None:
    # A newly enabled source must complete its initial full scan before Beat can launch
    # incremental/recheck work. This keeps the operator-controlled first scan deterministic.
    if operation in {"incremental", "recheck"} and not source.has_successful_full_scan:
        return None
    expression = _configured_schedule(source, operation)
    if not cron_expression_is_due(expression, now):
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
        run = _run_async(_get_or_create_queued_scan(source.source_id, scan_type))
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

    return _run_locked_periodic("matching", process_unprocessed_jobs(), ttl_seconds=900)


@celery_app.task(name="job_agent.scheduler.prepare_pending_applications")
def prepare_pending_applications_task() -> int | dict[str, str]:
    from app.applications.service import prepare_pending_applications

    return _run_locked_periodic(
        "prepare-applications",
        prepare_pending_applications(),
        ttl_seconds=900,
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


__all__ = [
    "DEFAULT_SOURCE_SCHEDULES",
    "close_task_event_loop",
    "cron_expression_is_due",
    "dispatch_due_sources_task",
    "generate_daily_report_task",
    "prepare_pending_applications_task",
    "process_unprocessed_jobs_task",
    "recheck_source_task",
    "record_learning_shadow_task",
    "retry_temporary_failures_task",
    "run_scan_task",
    "send_auto_approved_applications_task",
    "start_scan_task",
    "train_learning_models_task",
]
