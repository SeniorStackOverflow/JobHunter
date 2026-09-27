from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.scheduler.tasks as scheduler_tasks
from app.crawlers.pipeline import ScanService, _persistable_checkpoint
from app.crawlers.registry import JobSourceAdapterRegistry
from app.crawlers.schemas import ScanCheckpoint
from app.models.entities import JobSource, ScanRun
from app.models.enums import RunStatus, ScanType, SourceHealth


class FakeRedis:
    def __init__(self, *, live_lease: bool = False) -> None:
        self.live_lease = live_lease

    def exists(self, _key: str) -> int:
        return int(self.live_lease)


def _service(
    session_factory: async_sessionmaker[AsyncSession],
) -> ScanService:
    return ScanService(
        session_factory,
        cast(JobSourceAdapterRegistry, object()),
    )


async def _seed_running_scan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    heartbeat_age: timedelta,
) -> tuple[JobSource, ScanRun]:
    async with session_factory() as session:
        source = JobSource(
            name="fixture",
            base_url="https://fixture-site",
            adapter_type="generic_html",
            enabled=True,
            health_status=SourceHealth.HEALTHY,
        )
        session.add(source)
        await session.flush()
        run = ScanRun(
            source_id=source.id,
            scan_type=ScanType.INCREMENTAL,
            status=RunStatus.RUNNING,
            started_at=now - timedelta(minutes=20),
            heartbeat_at=now - heartbeat_age,
            owner_task_id="celery-task-old",
            checkpoint={
                "entrypoint_index": 2,
                "page_url": "https://fixture-site/page/3",
                "yielded_external_ids": ["1", "2"],
            },
        )
        session.add(run)
        await session.commit()
        return source, run


async def test_reconcile_orphaned_scan_without_live_lease(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    source, run = await _seed_running_scan(
        sqlite_session_factory,
        now=now,
        heartbeat_age=timedelta(minutes=10),
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "async_session_factory",
        sqlite_session_factory,
    )

    reconciled = await scheduler_tasks._reconcile_orphaned_scans(
        FakeRedis(live_lease=False),  # type: ignore[arg-type]
        now=now,
    )

    assert [item.id for item in reconciled] == [run.id]
    async with sqlite_session_factory() as session:
        stored = await session.get(ScanRun, run.id)
        stored_source = await session.get(JobSource, source.id)
        assert stored is not None
        assert stored.status == RunStatus.PARTIAL
        assert stored.finished_at is not None
        assert stored.owner_task_id is None
        assert stored.diagnostics["failure"] == "orphaned_worker"
        assert stored.diagnostics["orphaned_owner_task_id"] == "celery-task-old"
        assert stored.checkpoint["page_url"] == "https://fixture-site/page/3"
        assert stored_source is not None
        assert stored_source.health_status == SourceHealth.HEALTHY
        assert stored_source.last_scan_status == RunStatus.PARTIAL

    repeated = await scheduler_tasks._reconcile_orphaned_scans(
        FakeRedis(live_lease=False),  # type: ignore[arg-type]
        now=now + timedelta(minutes=5),
    )
    assert repeated == []


async def test_reconcile_does_not_touch_stale_scan_with_live_lease(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    _source, run = await _seed_running_scan(
        sqlite_session_factory,
        now=now,
        heartbeat_age=timedelta(minutes=10),
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "async_session_factory",
        sqlite_session_factory,
    )

    reconciled = await scheduler_tasks._reconcile_orphaned_scans(
        FakeRedis(live_lease=True),  # type: ignore[arg-type]
        now=now,
    )

    assert reconciled == []
    async with sqlite_session_factory() as session:
        stored = await session.get(ScanRun, run.id)
        assert stored is not None
        assert stored.status == RunStatus.RUNNING
        assert stored.owner_task_id == "celery-task-old"


async def test_running_scan_is_not_reenqueued_by_scheduler_helper(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    source, run = await _seed_running_scan(
        sqlite_session_factory,
        now=now,
        heartbeat_age=timedelta(seconds=30),
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "async_session_factory",
        sqlite_session_factory,
    )

    returned, should_enqueue = await scheduler_tasks._get_or_create_queued_scan(
        source.id,
        ScanType.INCREMENTAL,
    )

    assert returned.id == run.id
    assert should_enqueue is False


async def test_claim_scan_records_owner_and_rejects_second_owner(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        source = JobSource(
            name="fixture",
            base_url="https://fixture-site",
            adapter_type="generic_html",
            enabled=True,
        )
        session.add(source)
        await session.flush()
        run = ScanRun(
            source_id=source.id,
            scan_type=ScanType.INCREMENTAL,
            status=RunStatus.QUEUED,
        )
        session.add(run)
        await session.commit()

    service = _service(sqlite_session_factory)
    assert await service.claim_scan(run.id, "task-a") is True
    async with sqlite_session_factory() as session:
        stored = await session.get(ScanRun, run.id)
        assert stored is not None
        assert stored.owner_task_id == "task-a"
        assert stored.heartbeat_at is not None
        stored.status = RunStatus.RUNNING
        await session.commit()

    assert await service.claim_scan(run.id, "task-b") is False


def test_persistable_checkpoint_drops_runtime_known_state() -> None:
    raw = ScanCheckpoint(
        entrypoint_index=2,
        page_url="https://fixture-site/page/3",
        yielded_external_ids=["1", "2"],
        adapter_state={
            "known_external_ids": ["1", "2", "3"],
            "known_updated_hints": {"1": "x"},
            "known_last_checked_at": {"1": "2026-09-27T10:00:00+00:00"},
            "failed_reference_attempts": {"9": 1},
        },
    )

    persisted = _persistable_checkpoint(raw)

    assert persisted["entrypoint_index"] == 2
    assert persisted["page_url"] == "https://fixture-site/page/3"
    assert persisted["adapter_state"] == {"failed_reference_attempts": {"9": 1}}


async def test_runtime_timeout_marks_scan_partial() -> None:
    run = ScanRun(
        id=uuid4(),
        source_id=uuid4(),
        scan_type=ScanType.INCREMENTAL,
        status=RunStatus.RUNNING,
    )

    class SlowService:
        async def run_scan(self, _scan_id: Any) -> ScanRun:
            await asyncio.sleep(10)
            return run

        async def interrupt_scan(
            self,
            _scan_id: Any,
            *,
            reason: str,
            expected_owner_task_id: str | None,
            details: dict[str, Any],
        ) -> ScanRun:
            assert reason == "runtime_timeout"
            assert expected_owner_task_id == "task-a"
            assert details == {"timeout_seconds": 0.01}
            run.status = RunStatus.PARTIAL
            run.diagnostics = {"failure": reason}
            return run

    result = await scheduler_tasks._run_scan_with_timeout(
        cast(ScanService, SlowService()),
        run.id,
        owner_task_id="task-a",
        timeout_seconds=0.01,  # type: ignore[arg-type]
    )

    assert result.status == RunStatus.PARTIAL
    assert result.diagnostics["failure"] == "runtime_timeout"
