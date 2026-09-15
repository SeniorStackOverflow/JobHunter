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
from sqlalchemy import select

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
) -> ScanRun:
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
    return await _scan_service().create_scan(
        source_id,
        scan_type,
        actor="celery_beat",
        resume_from_checkpoint=resume_from_checkpoint,
    )


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


def _operation_allowed_for_source(source: SourceSchedule, operation: str) -> bool:
    # A degraded source may run its scheduled incremental scan as a bounded recovery probe.
    # Rechecks and full scans stay suppressed until a successful scan restores HEALTHY.
    return source.health_status != SourceHealth.DEGRADED or operation == "incremental"


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
        run = _run_async(
            _get_or_create_queued_scan(
                source.source_id,
                scan_type,
                resume_from_checkpoint=_resume_from_checkpoint_enabled(source, operation),
            )
        )
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
    "rabota_md_waf_canary_task",
    "recheck_source_task",
    "reconcile_phone_sms_task",
    "record_learning_shadow_task",
    "retry_temporary_failures_task",
    "run_scan_task",
    "send_auto_approved_applications_task",
    "start_scan_task",
    "train_learning_models_task",
]


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


def _rabota_md_uses_waf_http(source: JobSource) -> bool:
    configured = source.configuration.get("source", source.configuration)
    raw = configured if isinstance(configured, dict) else {}
    transport = raw.get("transport")
    if transport is None:
        transport = "stealth_browser" if raw.get("use_stealth_browser", True) else "waf_http"
    return transport == "waf_http"


async def _rabota_md_waf_pagination_probe(source: JobSource, token: str) -> None:
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
    from app.crawlers.browser import StealthPlaywrightBrowser
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
        user_agent=get_settings().crawler_user_agent,
        requests_per_minute=requests_per_minute,
        minimum_interval_seconds=minimum_interval_seconds,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        rate_limiter=limiter,
    )
    primary = WafHttpClient(secure, provider)
    browser = StealthPlaywrightBrowser(
        allowed_domains=(
            "rabota.md",
            "www.rabota.md",
            "token.awswaf.com",
            "captcha.awswaf.com",
        ),
        requests_per_minute=requests_per_minute,
        minimum_interval_seconds=minimum_interval_seconds,
        timeout_seconds=timeout_seconds,
        max_navigations_per_page=browser_max_navigations,
    )
    fetcher = FallbackFetcher(primary, browser, provider, max_switches_per_scan=1)
    try:
        response = await fetcher.post_html_fragment(page_url, referer=referer)
        if response.status_code != 200:
            raise RuntimeError(f"pagination probe returned HTTP {response.status_code}")
    finally:
        await fetcher.aclose()


async def _rabota_md_waf_canary() -> dict[str, str]:
    from redis.asyncio import Redis as AsyncRedis

    from app.crawlers.adapters.rabota_md.transport import WAF_SOLVER_USER_AGENT
    from app.crawlers.adapters.rabota_md.waf.solver import AwsWafSolver
    from app.crawlers.adapters.rabota_md.waf.watchdog import ScriptWatchdog
    from app.observability.metrics import WAF_SOLVER_CANARY, WAF_SOLVER_COMPATIBILITY

    source = await _rabota_md_waf_canary_source()
    if source is None or not _rabota_md_uses_waf_http(source):
        WAF_SOLVER_CANARY.labels(outcome="skipped").inc()
        return {"outcome": "skipped", "reason": "waf_http_not_enabled"}

    redis = AsyncRedis.from_url(get_settings().redis_url)
    try:
        watchdog = ScriptWatchdog(redis)
        solver = AwsWafSolver(script_hash_checker=lambda _digest: True)
        try:
            token = await solver.solve(source.base_url, WAF_SOLVER_USER_AGENT)
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
            await _rabota_md_waf_pagination_probe(source, token)
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
