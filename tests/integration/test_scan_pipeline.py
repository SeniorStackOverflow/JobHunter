from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crawlers.pipeline import ScanService
from app.crawlers.registry.registry import build_default_registry
from app.models.entities import Alert, CanonicalJob, JobSnapshot, JobSource, ScanRun, SourceJob
from app.models.enums import JobStatus, RunStatus, ScanType, SourceHealth


class FixtureSiteFetcher:
    """HTTP adapter that keeps every read inside the in-process fixture ASGI app."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def get(self, url: str, **_kwargs: object) -> httpx.Response:
        response = await self.client.get(url)
        if response.status_code == 410 and httpx.URL(url).path.endswith("/job/courier"):
            # Exercise the confirmed-absence state machine. A 410 is an explicit closure and
            # correctly closes immediately; a 404 must require repeated confirmation.
            return httpx.Response(404, request=response.request)
        return response


class AccessDeniedFetcher(FixtureSiteFetcher):
    async def get(self, url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(403, request=httpx.Request("GET", url))


class OneTimeDetailFailureFetcher(FixtureSiteFetcher):
    def __init__(self, client: httpx.AsyncClient) -> None:
        super().__init__(client)
        self.failed = False

    async def get(self, url: str, **_kwargs: object) -> httpx.Response:
        if not self.failed and httpx.URL(url).path.endswith("/job/courier"):
            self.failed = True
            raise httpx.ReadTimeout(
                "one-time timeout at https://user:password@example.test/jobs?token=secret"
            )
        return await super().get(url)


def make_source(
    configuration: dict[str, Any],
    *,
    name: str = "Generic fixture source",
) -> JobSource:
    return JobSource(
        name=name,
        base_url="https://fixture-site",
        adapter_type="generic_html",
        configuration=configuration,
        enabled=True,
        rate_limit=600,
        concurrency=2,
    )


def mirror_configuration(configuration: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(configuration)
    source = result["source"]
    source["id"] = "fixture_mirror"
    source["name"] = "Independent fixture mirror"
    source["locales"] = [
        {
            "code": "en",
            "start_urls": ["https://fixture-site/en/jobs?mirror=true"],
        }
    ]
    source["discovery"] = {
        "category_pages": [],
        "region_pages": [],
        "additional_entrypoints": [],
    }
    return result


async def persist_source(
    session_factory: async_sessionmaker[AsyncSession],
    source: JobSource,
) -> UUID:
    async with session_factory() as session:
        session.add(source)
        await session.commit()
        return source.id


async def run_full_scan(service: ScanService, source_id: UUID) -> ScanRun:
    queued = await service.create_scan(source_id, ScanType.FULL)
    return await service.run_scan(queued.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_scan_reuses_active_logical_scan(
    fixture_site_client: httpx.AsyncClient,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    generic_source_configuration: dict[str, Any],
) -> None:
    registry = build_default_registry(
        client_factory=lambda _source: FixtureSiteFetcher(fixture_site_client)
    )
    service = ScanService(sqlite_session_factory, registry)
    source_id = await persist_source(
        sqlite_session_factory,
        make_source(generic_source_configuration),
    )

    first = await service.create_scan(source_id, ScanType.FULL)
    duplicate = await service.create_scan(source_id, ScanType.FULL)

    assert duplicate.id == first.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_degradation_baseline_does_not_compare_full_and_incremental_scans(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    generic_source_configuration: dict[str, Any],
) -> None:
    service = ScanService(sqlite_session_factory, build_default_registry())
    source_id = await persist_source(
        sqlite_session_factory,
        make_source(generic_source_configuration, name="Scan baseline fixture"),
    )
    async with sqlite_session_factory() as session:
        previous_full = ScanRun(
            source_id=source_id,
            scan_type=ScanType.FULL,
            status=RunStatus.SUCCEEDED,
            found_jobs=1_000,
            finished_at=datetime.now(UTC),
        )
        current_incremental = ScanRun(
            source_id=source_id,
            scan_type=ScanType.INCREMENTAL,
            status=RunStatus.RUNNING,
            found_jobs=100,
        )
        session.add_all([previous_full, current_incremental])
        await session.flush()

        assert await service._detect_degradation(session, current_incremental) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_incomplete_full_scan_does_not_trigger_mass_drop_degradation(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    generic_source_configuration: dict[str, Any],
) -> None:
    service = ScanService(sqlite_session_factory, build_default_registry())
    source_id = await persist_source(
        sqlite_session_factory,
        make_source(generic_source_configuration, name="Interrupted full-scan fixture"),
    )
    async with sqlite_session_factory() as session:
        previous_full = ScanRun(
            source_id=source_id,
            scan_type=ScanType.FULL,
            status=RunStatus.SUCCEEDED,
            found_jobs=4_458,
            finished_at=datetime.now(UTC),
        )
        interrupted_full = ScanRun(
            source_id=source_id,
            scan_type=ScanType.FULL,
            status=RunStatus.RUNNING,
            found_jobs=815,
            updated_jobs=814,
            parsing_errors=1,
            checkpoint={
                "adapter_state": {"failed_reference_attempts": {"127405": 1}}
            },
        )
        session.add_all([previous_full, interrupted_full])
        await session.flush()

        assert await service._detect_degradation(session, interrupted_full) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_detail_is_retried_from_checkpoint_without_data_loss(
    fixture_site_client: httpx.AsyncClient,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    generic_source_configuration: dict[str, Any],
) -> None:
    fetcher = OneTimeDetailFailureFetcher(fixture_site_client)
    service = ScanService(
        sqlite_session_factory,
        build_default_registry(client_factory=lambda _source: fetcher),
    )
    source_id = await persist_source(
        sqlite_session_factory,
        make_source(generic_source_configuration, name="Checkpoint retry fixture"),
    )

    interrupted = await run_full_scan(service, source_id)
    assert interrupted.status == RunStatus.PARTIAL
    assert interrupted.parsing_errors == 1
    assert interrupted.diagnostics["errors"] == [{"external_id": "fx-002", "type": "ReadTimeout"}]
    assert "password" not in str(interrupted.diagnostics)
    assert "secret" not in str(interrupted.diagnostics)
    assert "fx-002" not in interrupted.checkpoint["yielded_external_ids"]
    assert interrupted.checkpoint["adapter_state"]["failed_reference_attempts"] == {"fx-002": 1}

    resumed = await run_full_scan(service, source_id)
    assert resumed.status == RunStatus.SUCCEEDED
    assert resumed.checkpoint["adapter_state"]["failed_reference_attempts"] == {}
    async with sqlite_session_factory() as session:
        ids = set(
            (
                await session.scalars(
                    select(SourceJob.external_job_id).where(SourceJob.source_id == source_id)
                )
            ).all()
        )
    assert ids == {f"fx-{index:03d}" for index in range(1, 10)} | {"fx-011"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_incremental_short_page_budget_does_not_count_discovery_metadata_as_updates(
    fixture_site_client: httpx.AsyncClient,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    generic_source_configuration: dict[str, Any],
) -> None:
    configuration = deepcopy(generic_source_configuration)
    configuration["source"]["incremental_scan"]["max_pages_per_entrypoint"] = 2
    fetcher = FixtureSiteFetcher(fixture_site_client)
    service = ScanService(
        sqlite_session_factory,
        build_default_registry(client_factory=lambda _source: fetcher),
    )
    source_id = await persist_source(
        sqlite_session_factory,
        make_source(configuration, name="Short incremental budget fixture"),
    )

    phase_one = await run_full_scan(service, source_id)
    assert phase_one.status == RunStatus.SUCCEEDED
    assert phase_one.new_jobs == 10

    control_response = await fixture_site_client.post("/__control__/phase/2")
    assert control_response.status_code == 200
    queued = await service.create_scan(source_id, ScanType.INCREMENTAL)
    incremental = await service.run_scan(queued.id)

    assert incremental.status == RunStatus.SUCCEEDED
    assert incremental.new_jobs == 1
    assert incremental.updated_jobs == 1
    assert incremental.unchanged_jobs == 9


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_update_deduplicate_checkpoint_and_recheck_pipeline(
    fixture_site_client: httpx.AsyncClient,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    generic_source_configuration: dict[str, Any],
) -> None:
    fetcher = FixtureSiteFetcher(fixture_site_client)
    registry = build_default_registry(client_factory=lambda _source: fetcher)
    service = ScanService(sqlite_session_factory, registry)
    primary_source_id = await persist_source(
        sqlite_session_factory,
        make_source(generic_source_configuration),
    )

    phase_one = await run_full_scan(service, primary_source_id)

    assert phase_one.status == RunStatus.SUCCEEDED
    assert phase_one.found_jobs == 10
    assert phase_one.new_jobs == 10
    assert phase_one.updated_jobs == 0
    assert phase_one.parsing_errors == 0
    assert phase_one.checkpoint["yielded_external_ids"] == [
        f"fx-{index:03d}" for index in range(1, 10)
    ] + ["fx-011"]
    async with sqlite_session_factory() as session:
        phase_one_jobs = list(
            (
                await session.scalars(
                    select(SourceJob).where(SourceJob.source_id == primary_source_id)
                )
            ).all()
        )
        old_job = next(job for job in phase_one_jobs if job.external_job_id == "fx-001")
        recent_job = next(job for job in phase_one_jobs if job.external_job_id == "fx-002")
        assert len(phase_one_jobs) == 10
        assert old_job.published_at is not None and old_job.published_at.year == 2024
        assert recent_job.published_at is not None and recent_job.published_at.year == 2026
        assert old_job.localized_urls == {"ro": "https://fixture-site/ro/job/security-engineer"}
        assert await session.scalar(select(func.count(CanonicalJob.id))) == 10

    control_response = await fixture_site_client.post("/__control__/phase/2")
    assert control_response.status_code == 200
    phase_two = await run_full_scan(service, primary_source_id)

    assert phase_two.status == RunStatus.SUCCEEDED
    assert phase_two.found_jobs == 11
    assert phase_two.new_jobs == 1
    assert phase_two.updated_jobs == 1
    assert phase_two.unchanged_jobs == 9
    async with sqlite_session_factory() as session:
        security_job = await session.scalar(
            select(SourceJob).where(
                SourceJob.source_id == primary_source_id,
                SourceJob.external_job_id == "fx-001",
            )
        )
        assert security_job is not None
        assert str(security_job.salary_min) == "30000.00"
        assert str(security_job.salary_max) == "40000.00"
        snapshots = list(
            (
                await session.scalars(
                    select(JobSnapshot).where(JobSnapshot.source_job_id == security_job.id)
                )
            ).all()
        )
        assert len(snapshots) == 1
        assert {"salary_text", "salary_min", "salary_max"} <= set(snapshots[0].changed_fields)
        assert snapshots[0].requires_rematch is True
        assert snapshots[0].salary == {
            "text": "30000 - 40000 MDL",
            "minimum": "30000",
            "maximum": "40000",
            "currency": "MDL",
        }
        assert (
            await session.scalar(
                select(func.count(SourceJob.id)).where(SourceJob.source_id == primary_source_id)
            )
            == 11
        )
        assert await session.scalar(select(func.count(CanonicalJob.id))) == 11

    mirror_source_id = await persist_source(
        sqlite_session_factory,
        make_source(
            mirror_configuration(generic_source_configuration),
            name="Independent fixture mirror",
        ),
    )
    mirror_scan = await run_full_scan(service, mirror_source_id)

    assert mirror_scan.status == RunStatus.SUCCEEDED
    assert mirror_scan.found_jobs == 1
    assert mirror_scan.new_jobs == 1
    async with sqlite_session_factory() as session:
        primary_backend = await session.scalar(
            select(SourceJob).where(
                SourceJob.source_id == primary_source_id,
                SourceJob.external_job_id == "fx-011",
            )
        )
        mirror_backend = await session.scalar(
            select(SourceJob).where(
                SourceJob.source_id == mirror_source_id,
                SourceJob.external_job_id == "fx-011",
            )
        )
        assert primary_backend is not None
        assert mirror_backend is not None
        assert mirror_backend.id != primary_backend.id
        assert mirror_backend.canonical_job_id == primary_backend.canonical_job_id
        assert await session.scalar(select(func.count(SourceJob.id))) == 12
        assert await session.scalar(select(func.count(CanonicalJob.id))) == 11

    control_response = await fixture_site_client.post("/__control__/phase/3")
    assert control_response.status_code == 200
    first_recheck = await service.recheck_active_jobs(
        primary_source_id,
        close_after_confirmed_absence_count=2,
    )

    assert first_recheck == {
        "checked": 11,
        "updated": 0,
        "possibly_closed": 1,
        "closed": 0,
        "errors": 0,
    }
    async with sqlite_session_factory() as session:
        courier = await session.scalar(
            select(SourceJob).where(
                SourceJob.source_id == primary_source_id,
                SourceJob.external_job_id == "fx-002",
            )
        )
        assert courier is not None
        assert courier.status == JobStatus.POSSIBLY_CLOSED
        assert courier.confirmed_absence_count == 1

    second_recheck = await service.recheck_active_jobs(
        primary_source_id,
        close_after_confirmed_absence_count=2,
    )

    assert second_recheck == {
        "checked": 11,
        "updated": 0,
        "possibly_closed": 0,
        "closed": 1,
        "errors": 0,
    }
    async with sqlite_session_factory() as session:
        courier = await session.scalar(
            select(SourceJob).where(
                SourceJob.source_id == primary_source_id,
                SourceJob.external_job_id == "fx-002",
            )
        )
        assert courier is not None
        assert courier.status == JobStatus.CLOSED
        assert courier.confirmed_absence_count == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_access_degradation_pauses_only_the_affected_source(
    fixture_site_client: httpx.AsyncClient,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    generic_source_configuration: dict[str, Any],
) -> None:
    source_id = await persist_source(
        sqlite_session_factory,
        make_source(generic_source_configuration, name="Blocked fixture source"),
    )
    registry = build_default_registry(
        client_factory=lambda _source: AccessDeniedFetcher(fixture_site_client)
    )
    service = ScanService(sqlite_session_factory, registry)

    run = await run_full_scan(service, source_id)

    assert run.status == RunStatus.PARTIAL
    async with sqlite_session_factory() as session:
        stored_source = await session.get(JobSource, source_id)
        assert stored_source is not None
        assert stored_source.health_status == SourceHealth.DEGRADED
        assert stored_source.automatic_actions_paused is True
        alert = await session.scalar(select(Alert).where(Alert.source_id == source_id))
        assert alert is not None
        assert alert.code == "adapter_access_degraded"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_successful_scan_recovers_automatic_pause_after_degradation(
    fixture_site_client: httpx.AsyncClient,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    generic_source_configuration: dict[str, Any],
) -> None:
    source = make_source(generic_source_configuration, name="Recovery fixture source")
    source.health_status = SourceHealth.DEGRADED
    source.automatic_actions_paused = True
    source_id = await persist_source(sqlite_session_factory, source)
    registry = build_default_registry(
        client_factory=lambda _source: FixtureSiteFetcher(fixture_site_client)
    )
    service = ScanService(sqlite_session_factory, registry)

    run = await run_full_scan(service, source_id)

    assert run.status == RunStatus.SUCCEEDED
    async with sqlite_session_factory() as session:
        stored_source = await session.get(JobSource, source_id)
        assert stored_source is not None
        assert stored_source.health_status == SourceHealth.HEALTHY
        assert stored_source.automatic_actions_paused is False
