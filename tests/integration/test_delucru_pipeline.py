from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.crawlers.pipeline import ScanService
from app.crawlers.registry import build_default_registry
from app.models.entities import Alert, JobSnapshot, JobSource, ScanRun, SourceJob
from app.models.enums import RunStatus, ScanType, SourceHealth
from tests.unit.test_delucru_adapter import (
    BASE,
    FixtureFetcher,
    default_routes,
    finite_category_routes,
    fixture,
)


async def source_record(factory, **overrides):
    async with factory() as session:
        source = JobSource(
            name="Delucru offline",
            adapter_type="delucru_md",
            base_url=BASE,
            configuration={"live_mode": False, "locale_priority": ["ro"], **overrides},
            enabled=True,
            health_status=SourceHealth.HEALTHY,
            automatic_actions_paused=False,
        )
        session.add(source)
        await session.commit()
        return source.id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("stage", ["validation", "categories", "regions", "details"])
async def test_delucru_blocked_access_pauses_source_at_every_stage(
    sqlite_session_factory, status, stage
):
    routes = default_routes()
    url = {
        "validation": f"{BASE}/jobs",
        "categories": f"{BASE}/jobs/by-category",
        "regions": f"{BASE}/jobs/by-city",
        "details": f"{BASE}/job/junior-data-scientist-88409",
    }[stage]
    routes[url] = (status, "Blocked")
    source_id = await source_record(sqlite_session_factory)
    scanner = ScanService(
        sqlite_session_factory,
        build_default_registry(client_factory=lambda _: FixtureFetcher(routes)),
    )
    queued = await scanner.create_scan(source_id, ScanType.FULL)
    run = await scanner.run_scan(queued.id)
    assert run.status in {RunStatus.PARTIAL, RunStatus.FAILED}
    async with sqlite_session_factory() as session:
        source = await session.get(JobSource, source_id)
        assert source.health_status is SourceHealth.DEGRADED
        assert source.automatic_actions_paused
        if stage != "validation":
            assert await session.scalar(select(Alert.id).where(Alert.source_id == source_id))


@pytest.mark.asyncio
async def test_category_only_jobs_and_metadata_merge_without_duplicate_details(
    sqlite_session_factory,
):
    routes = default_routes()
    routes[f"{BASE}/jobs/medicine-pharmacy"] = (
        '<html><a href="/job/99999">Additional vacancy</a></html>'
    )
    routes[f"{BASE}/job/99999"] = fixture("job_88409_it.html")
    fetcher = FixtureFetcher(routes)
    source_id = await source_record(sqlite_session_factory)
    scanner = ScanService(
        sqlite_session_factory, build_default_registry(client_factory=lambda _: fetcher)
    )
    run = await scanner.run_scan((await scanner.create_scan(source_id, ScanType.FULL)).id)
    assert run.status is RunStatus.SUCCEEDED
    assert run.new_jobs == 4
    async with sqlite_session_factory() as session:
        assert await session.scalar(select(func.count(SourceJob.id))) == 4
        job = await session.scalar(select(SourceJob).where(SourceJob.external_job_id == "99999"))
        assert job.category == "medicine-pharmacy"
        primary = await session.scalar(
            select(SourceJob).where(SourceJob.external_job_id == "88409")
        )
        assert primary.category is not None
        assert len(primary.categories_seen) >= 3
    assert fetcher.requested.count(f"{BASE}/job/junior-data-scientist-88409") == 1


@pytest.mark.asyncio
async def test_description_edit_persists_snapshot_and_requires_rematch(sqlite_session_factory):
    routes = default_routes()
    fetcher = FixtureFetcher(routes)
    source_id = await source_record(sqlite_session_factory)
    scanner = ScanService(
        sqlite_session_factory, build_default_registry(client_factory=lambda _: fetcher)
    )
    await scanner.run_scan((await scanner.create_scan(source_id, ScanType.FULL)).id)
    changed = fixture("job_88409_it.html").replace(
        "Python 3.12, SQL, machine learning basics.", "Mandatory forklift certificate."
    )
    routes[f"{BASE}/job/junior-data-scientist-88409"] = changed
    run = await scanner.run_scan((await scanner.create_scan(source_id, ScanType.FULL)).id)
    assert run.updated_jobs == 1
    async with sqlite_session_factory() as session:
        job = await session.scalar(select(SourceJob).where(SourceJob.external_job_id == "88409"))
        assert "Mandatory forklift certificate" in job.description
        snapshot = await session.scalar(
            select(JobSnapshot).where(JobSnapshot.source_job_id == job.id)
        )
        assert snapshot.requires_rematch
        assert {"description", "requirements"} <= set(snapshot.changed_fields)


@pytest.mark.asyncio
async def test_page_cap_is_partial_and_resume_completes_remaining_pages(sqlite_session_factory):
    routes = default_routes()
    routes[f"{BASE}/jobs/it-internet"] = fixture("empty_listing.html")
    routes[f"{BASE}/jobs/food-industry-horeca"] = fixture("empty_listing.html")
    routes[f"{BASE}/jobs/sales-consulting"] = fixture("empty_listing.html")
    routes[f"{BASE}/jobs?page=2"] = '<html><a href="/job/99999">Additional vacancy</a></html>'
    routes[f"{BASE}/job/99999"] = fixture("job_88409_it.html")
    source_id = await source_record(sqlite_session_factory, max_pages_per_entrypoint=1)
    scanner = ScanService(
        sqlite_session_factory,
        build_default_registry(client_factory=lambda _: FixtureFetcher(routes)),
    )
    first = await scanner.run_scan((await scanner.create_scan(source_id, ScanType.FULL)).id)
    assert first.status is RunStatus.PARTIAL
    assert first.checkpoint["page_url"] == f"{BASE}/jobs?page=2"
    second = await scanner.run_scan(
        (await scanner.create_scan(source_id, ScanType.FULL, resume_scan_id=first.id)).id
    )
    assert second.status is RunStatus.SUCCEEDED
    assert second.new_jobs == 1
    async with sqlite_session_factory() as session:
        assert await session.scalar(select(func.count(SourceJob.id))) == 4


async def test_manual_full_continues_saved_cursor_without_refetching_committed_jobs(
    sqlite_session_factory,
):
    from tests.unit.test_delucru_adapter import finite_category_routes

    routes = finite_category_routes()
    page2 = f"{BASE}/jobs/acquisitions?page=2"
    saved_last = routes[page2]
    routes[page2] = (403, "Temporarily blocked")
    fetcher = FixtureFetcher(routes)
    source_id = await source_record(sqlite_session_factory)
    scanner = ScanService(
        sqlite_session_factory, build_default_registry(client_factory=lambda _: fetcher)
    )
    first, created = await scanner.request_manual_scan(source_id, ScanType.FULL)
    assert created
    partial = await scanner.run_scan(first.id)
    assert partial.status == RunStatus.PARTIAL
    assert partial.checkpoint["page_url"] == page2
    assert partial.new_jobs == 1
    routes[page2] = saved_last
    continued, created = await scanner.request_manual_scan(source_id, ScanType.FULL)
    assert created and continued.id != first.id
    assert continued.checkpoint["page_url"] == page2
    assert continued.diagnostics["resume_parent_scan_id"] == str(first.id)
    duplicate, created = await scanner.request_manual_scan(source_id, ScanType.INCREMENTAL)
    assert not created and duplicate.id == continued.id
    completed = await scanner.run_scan(continued.id)
    assert completed.status == RunStatus.SUCCEEDED and completed.new_jobs == 2
    assert fetcher.requested.count(f"{BASE}/job/junior-data-scientist-88409") == 1
    assert f"{BASE}/jobs/acquisitions?page=3" not in fetcher.requested
    async with sqlite_session_factory() as session:
        assert await session.scalar(select(func.count(SourceJob.id))) == 3
        source = await session.get(JobSource, source_id)
        assert source.health_status == SourceHealth.HEALTHY


async def test_manual_full_after_success_does_not_resume_older_partial_cursor(
    sqlite_session_factory,
):
    fetcher = FixtureFetcher(default_routes())
    source_id = await source_record(sqlite_session_factory)
    scanner = ScanService(
        sqlite_session_factory, build_default_registry(client_factory=lambda _: fetcher)
    )
    first = await scanner.run_scan((await scanner.create_scan(source_id, ScanType.FULL)).id)
    async with sqlite_session_factory() as session:
        old = await session.get(ScanRun, first.id)
        old.status = RunStatus.PARTIAL
        old.checkpoint = {"page_url": f"{BASE}/jobs?page=999", "yielded_external_ids": ["88409"]}
        await session.commit()
    succeeded = await scanner.run_scan((await scanner.create_scan(source_id, ScanType.FULL)).id)
    assert succeeded.status == RunStatus.SUCCEEDED
    fresh, created = await scanner.request_manual_scan(source_id, ScanType.FULL)
    assert created and fresh.checkpoint == {}
    assert "resume_parent_scan_id" not in fresh.diagnostics
    completed = await scanner.run_scan(fresh.id)
    assert completed.status == RunStatus.SUCCEEDED and completed.found_jobs == 3
    assert fetcher.requested.count(f"{BASE}/job/junior-data-scientist-88409") == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("scan_type", [ScanType.FULL, ScanType.INCREMENTAL])
async def test_legacy_detail_fields_refreshed_on_resume_and_fresh_incremental(
    sqlite_session_factory, scan_type
):
    routes = finite_category_routes()
    detail_url = f"{BASE}/job/junior-data-scientist-88409"
    current_detail = fixture("job_current_detail_ro.html")
    routes[detail_url] = current_detail.replace(
        'class="employer-details-page"', 'class="old-layout"'
    )
    page2 = f"{BASE}/jobs/acquisitions?page=2"
    saved_page2 = routes[page2]
    routes[page2] = (403, "Temporarily blocked")
    fetcher = FixtureFetcher(routes)
    source_id = await source_record(sqlite_session_factory, incremental_detail_refresh_budget=1)
    scanner = ScanService(
        sqlite_session_factory, build_default_registry(client_factory=lambda _: fetcher)
    )
    first = await scanner.run_scan((await scanner.create_scan(source_id, ScanType.FULL)).id)
    assert first.status == RunStatus.PARTIAL and first.new_jobs == 1
    async with sqlite_session_factory() as session:
        old = await session.scalar(select(SourceJob).where(SourceJob.external_job_id == "88409"))
        assert old.salary_text is None and old.no_experience is None and old.location is None
        old.raw_metadata = {
            key: value
            for key, value in old.raw_metadata.items()
            if key != "detail_normalization_version"
        }
        await session.commit()
    routes[page2] = saved_page2
    routes[detail_url] = current_detail
    if scan_type == ScanType.FULL:
        queued, created = await scanner.request_manual_scan(source_id, scan_type)
        assert created and queued.diagnostics["resume_parent_scan_id"] == str(first.id)
        assert "88409" in queued.checkpoint["yielded_external_ids"]
    else:
        queued = await scanner.create_scan(source_id, scan_type)
    repaired = await scanner.run_scan(queued.id)
    assert repaired.status == RunStatus.SUCCEEDED
    assert repaired.new_jobs == 2 and repaired.updated_jobs == 1
    assert "detail_normalization_refresh_ids" not in repaired.checkpoint["adapter_state"]
    async with sqlite_session_factory() as session:
        job = await session.scalar(select(SourceJob).where(SourceJob.external_job_id == "88409"))
        assert job.salary_min == Decimal("12000") and job.salary_max == Decimal("15000")
        assert job.currency == "MDL" and job.no_experience is True
        assert job.location == "Chișinău" and job.employment_type == "full-time"
        assert job.raw_metadata["detail_normalization_version"] == 1
        assert await session.scalar(select(func.count(SourceJob.id))) == 3
        snapshot = await session.scalar(
            select(JobSnapshot).where(JobSnapshot.source_job_id == job.id)
        )
        assert snapshot.requires_rematch
        assert {"salary_text", "salary_min", "no_experience", "location"} <= set(
            snapshot.changed_fields
        )
    assert fetcher.requested.count(detail_url) == 2
    fresh = await scanner.run_scan((await scanner.create_scan(source_id, ScanType.INCREMENTAL)).id)
    assert fresh.status == RunStatus.SUCCEEDED and fresh.new_jobs == 0
    assert fetcher.requested.count(detail_url) == 2
