# mypy: disable-error-code="untyped-decorator"
from __future__ import annotations

import asyncio
import time
from typing import Any

from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    setup_logging,
    task_failure,
    task_postrun,
    task_prerun,
    worker_process_init,
    worker_process_shutdown,
)

from app.observability import bind_log_context, clear_log_context, configure_logging
from app.observability.metrics import CELERY_TASK_DURATION, CELERY_TASKS
from app.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "job-agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.scheduler.tasks"],
)

celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    enable_utc=True,
    timezone="UTC",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_hijack_root_logger=False,
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        "visibility_timeout": 86_400,
        "global_keyprefix": "job-agent:",
    },
    result_backend_transport_options={
        "global_keyprefix": "job-agent-result:",
        "visibility_timeout": 86_400,
    },
    visibility_timeout=86_400,
    result_expires=86_400,
    task_track_started=True,
    task_time_limit=21_600,
    task_soft_time_limit=21_300,
    worker_send_task_events=True,
    task_send_sent_event=True,
    beat_schedule={
        "dispatch-per-source-schedules": {
            "task": "job_agent.scheduler.dispatch_due_sources",
            "schedule": crontab(minute="*"),
            "options": {"queue": "maintenance", "expires": 55},
        },
        "process-unprocessed-jobs": {
            "task": "job_agent.scheduler.process_unprocessed_jobs",
            "schedule": 300.0,
            "options": {"queue": "matching", "expires": 270},
        },
        "prepare-pending-applications": {
            "task": "job_agent.scheduler.prepare_pending_applications",
            "schedule": 300.0,
            "options": {"queue": "applications", "expires": 270},
        },
        "send-auto-approved-applications": {
            "task": "job_agent.scheduler.send_auto_approved_applications",
            "schedule": 60.0,
            "options": {"queue": "email", "expires": 55},
        },
        "retry-temporary-email-failures": {
            "task": "job_agent.scheduler.retry_temporary_failures",
            "schedule": 900.0,
            "options": {"queue": "email", "expires": 840},
        },
        "daily-report": {
            "task": "job_agent.scheduler.generate_daily_report",
            "schedule": crontab(minute=15, hour=21),
            "options": {"queue": "reports"},
        },
    },
    task_routes={
        "job_agent.scheduler.run_scan": {"queue": "crawling"},
        "job_agent.scheduler.start_scan": {"queue": "crawling"},
        "job_agent.scheduler.recheck_source": {"queue": "crawling"},
        "job_agent.scheduler.process_unprocessed_jobs": {"queue": "matching"},
        "job_agent.scheduler.prepare_pending_applications": {"queue": "applications"},
        "job_agent.scheduler.send_auto_approved_applications": {"queue": "email"},
        "job_agent.scheduler.retry_temporary_failures": {"queue": "email"},
        "job_agent.scheduler.generate_daily_report": {"queue": "reports"},
    },
)


@setup_logging.connect
def configure_celery_logging(**_kwargs: Any) -> None:
    configure_logging()


@worker_process_init.connect
def configure_worker_logging(**_kwargs: Any) -> None:
    from app.database import engine

    asyncio.run(engine.dispose())
    configure_logging()


@worker_process_shutdown.connect
def close_worker_resources(**_kwargs: Any) -> None:
    from app.scheduler.tasks import close_task_event_loop

    close_task_event_loop()


@task_prerun.connect
def observe_task_start(
    *,
    task_id: str | None = None,
    task: Any = None,
    **_kwargs: Any,
) -> None:
    clear_log_context()
    bind_log_context(correlation_id=task_id)
    if task is not None:
        task.request.job_agent_started_monotonic = time.monotonic()


@task_postrun.connect
def observe_task_end(
    *,
    task: Any = None,
    state: str | None = None,
    **_kwargs: Any,
) -> None:
    task_name = getattr(task, "name", "unknown")
    stable_state = (state or "unknown").lower()
    CELERY_TASKS.labels(task_name, stable_state).inc()
    started = getattr(getattr(task, "request", None), "job_agent_started_monotonic", None)
    if isinstance(started, (int, float)):
        CELERY_TASK_DURATION.labels(task_name).observe(max(0.0, time.monotonic() - started))
    clear_log_context()


@task_failure.connect
def observe_task_failure(*, task_id: str | None = None, **_kwargs: Any) -> None:
    bind_log_context(correlation_id=task_id)


__all__ = ["celery_app"]
