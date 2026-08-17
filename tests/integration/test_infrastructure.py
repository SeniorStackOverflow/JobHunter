from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from uuid import uuid4

import pytest
from redis import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.scheduler.locks import RedisTokenLock, lock_key

SERVICES_ENABLED = os.environ.get("RUN_SERVICE_INTEGRATION_TESTS") == "1"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not SERVICES_ENABLED,
        reason="set RUN_SERVICE_INTEGRATION_TESTS=1 with PostgreSQL and Redis available",
    ),
]


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required when service integration tests are enabled")
    return value


@pytest.mark.asyncio
async def test_postgres_is_reachable_and_migrations_are_applied() -> None:
    database_url = _required_environment("DATABASE_URL")
    if not database_url.startswith("postgresql+asyncpg://"):
        pytest.fail("service integration tests require a postgresql+asyncpg DATABASE_URL")

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert isinstance(revision, str)
            assert revision
    finally:
        await engine.dispose()


def test_redis_lock_uses_owner_token_for_extend_and_release() -> None:
    client = Redis.from_url(_required_environment("REDIS_URL"), decode_responses=True)
    key = lock_key("integration", uuid4().hex)
    try:
        assert client.ping()
        owner = RedisTokenLock(client=client, key=key, ttl_seconds=10)
        contender = RedisTokenLock(client=client, key=key, ttl_seconds=10)
        assert owner.acquire()
        assert not contender.acquire()
        assert owner.extend()

        client.set(key, "different-owner", ex=10)
        assert not owner.release()
        assert client.get(key) == "different-owner"
    finally:
        client.delete(key)
        client.connection_pool.disconnect()


def test_celery_beat_and_worker_safety_configuration() -> None:
    from app.scheduler import tasks as scheduler_tasks  # noqa: F401
    from app.scheduler.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    expected_periodic_tasks = {
        "job_agent.scheduler.dispatch_due_sources",
        "job_agent.scheduler.process_unprocessed_jobs",
        "job_agent.scheduler.prepare_pending_applications",
        "job_agent.scheduler.send_auto_approved_applications",
        "job_agent.scheduler.retry_temporary_failures",
        "job_agent.scheduler.generate_daily_report",
    }
    assert {entry["task"] for entry in schedule.values()} == expected_periodic_tasks
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.result_serializer == "json"
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.broker_transport_options["visibility_timeout"] == 86_400


@pytest.fixture(scope="module")
def celery_worker() -> Iterator[tuple[object, str, str]]:
    _required_environment("DATABASE_URL")
    _required_environment("REDIS_URL")
    from app.scheduler.celery_app import celery_app

    node_name = f"integration-{uuid4().hex[:10]}@job-agent-test"
    queue_name = f"integration-{uuid4().hex[:10]}"
    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "app.scheduler.celery_app:celery_app",
        "worker",
        "--pool=solo",
        "--concurrency=1",
        f"--hostname={node_name}",
        f"--queues={queue_name}",
        "--without-gossip",
        "--without-mingle",
        "--without-heartbeat",
        "--loglevel=WARNING",
    ]
    process = subprocess.Popen(  # noqa: S603
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ.copy(),
    )
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                pytest.fail(f"Celery integration worker exited during startup:\n{output[-4000:]}")
            replies = celery_app.control.ping(destination=[node_name], timeout=1)
            if any(node_name in reply for reply in replies):
                break
            time.sleep(0.25)
        else:
            process.terminate()
            output = process.communicate(timeout=10)[0]
            pytest.fail(f"Celery integration worker did not answer ping:\n{output[-4000:]}")
        yield celery_app, node_name, queue_name
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


def test_real_celery_worker_registers_and_executes_scheduler_task(
    celery_worker: tuple[object, str, str],
) -> None:
    celery_app, node_name, queue_name = celery_worker
    inspect = celery_app.control.inspect(destination=[node_name], timeout=5)
    registered = inspect.registered() or {}
    assert "job_agent.scheduler.dispatch_due_sources" in registered[node_name]

    result = celery_app.send_task(
        "job_agent.scheduler.dispatch_due_sources",
        queue=queue_name,
    )
    payload = result.get(timeout=20)
    assert payload["checked_sources"] >= 0
    assert isinstance(payload["dispatched"], list)
    result.forget()


def test_worker_can_execute_multiple_async_service_tasks_without_loop_reuse_failure(
    celery_worker: tuple[object, str, str],
) -> None:
    celery_app, _, queue_name = celery_worker
    first = celery_app.send_task(
        "job_agent.scheduler.dispatch_due_sources",
        queue=queue_name,
    )
    second = celery_app.send_task(
        "job_agent.scheduler.dispatch_due_sources",
        queue=queue_name,
    )
    first_payload = first.get(timeout=20)
    second_payload = second.get(timeout=20)
    assert first_payload["checked_sources"] >= 0
    assert second_payload["checked_sources"] >= 0
    first.forget()
    second.forget()
