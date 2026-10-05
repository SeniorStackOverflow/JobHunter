from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crawlers.catalog import reconcile_source_catalog
from app.crawlers.registry import build_default_registry
from app.database.base import Base
from app.deduplication import DeduplicationService
from app.email.providers import FakeGmailProvider
from app.email.service import EmailSendBlocked, EmailService
from app.models.entities import Application, CanonicalJob, JobSource
from app.models.enums import ApplicationStatus
from tests.integration.test_duplicate_delivery import legacy_duplicate_graph
from tests.unit.test_deduplication import job, source
from tests.unit.test_policy_and_email import settings


@pytest_asyncio.fixture
async def dedup_postgres():
    configured = os.environ.get("JOBHUNTER_DEDUP_TEST_DATABASE_URL")
    if not configured:
        pytest.skip("requires an isolated local jobhunter_dedup_test database")
    url = make_url(configured)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "jobhunter_dedup_test":
        raise ValueError("dedup tests require a dedicated local jobhunter_dedup_test database")
    schema = f"dedup_{uuid4().hex}"
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_postgres_source_catalog_startups_register_each_site_once(dedup_postgres):
    async with dedup_postgres() as session:
        legacy = JobSource(
            name="Operator's Rabota",
            base_url="https://www.rabota.md",
            adapter_type="rabota_md",
            configuration={"operator_setting": "keep"},
            enabled=True,
            automatic_actions_paused=False,
        )
        session.add(legacy)
        await session.commit()
        original_id = legacy.id
    barrier = asyncio.Barrier(6)
    registry = build_default_registry()

    async def startup():
        async with dedup_postgres() as session:
            await barrier.wait()
            created = await reconcile_source_catalog(session, registry)
            await session.commit()
            return created

    results = await asyncio.wait_for(asyncio.gather(*(startup() for _ in range(6))), timeout=30)
    assert sum(result.count("delucru_md") for result in results) == 1
    async with dedup_postgres() as session:
        assert await session.scalar(select(func.count(JobSource.id))) == len(
            registry.source_definitions()
        )
        legacy = await session.get(JobSource, original_id)
        assert legacy is not None and legacy.catalog_key == "rabota_md"
        assert legacy.enabled and not legacy.automatic_actions_paused
        assert legacy.configuration == {"operator_setting": "keep"}


@pytest.mark.asyncio
async def test_concurrent_postgres_assignments_share_one_canonical(dedup_postgres):
    async with dedup_postgres() as session:
        sources = [source("one"), source("two")]
        session.add_all(sources)
        await session.commit()
        ids = [src.id for src in sources]
    barrier = asyncio.Barrier(2)

    async def assign(index: int):
        async with dedup_postgres() as session:
            item = job(ids[index], str(index), "Warehouse Operator")
            session.add(item)
            await session.flush()
            await barrier.wait()
            result = await DeduplicationService().assign(session, item)
            await session.commit()
            return result.canonical_job.id

    first, second = await asyncio.wait_for(asyncio.gather(assign(0), assign(1)), timeout=20)
    assert first == second
    async with dedup_postgres() as session:
        assert await session.scalar(select(func.count(CanonicalJob.id))) == 1


@pytest.mark.asyncio
async def test_concurrent_postgres_legacy_senders_cannot_double_deliver(
    dedup_postgres, tmp_path: Path
):
    async with dedup_postgres() as session:
        ids = await legacy_duplicate_graph(session, tmp_path)
        await session.commit()
    blocked = asyncio.Event()

    class HeldFakeProvider(FakeGmailProvider):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def send(self, message):
            self.entered.set()
            await self.release.wait()
            return await super().send(message)

    provider = HeldFakeProvider()
    sender = EmailService(settings(tmp_path), dedup_postgres, provider)

    async def send(application_id):
        try:
            return await sender.send_application(application_id)
        except EmailSendBlocked:
            blocked.set()
            return None

    tasks = [asyncio.create_task(send(value)) for value in ids]
    try:
        await asyncio.wait_for(provider.entered.wait(), timeout=15)
        await asyncio.wait_for(blocked.wait(), timeout=15)
    finally:
        provider.release.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
    assert sum(value is not None for value in results) == 1
    assert len(provider.outbox) == 1
    async with dedup_postgres() as session:
        apps = list((await session.scalars(select(Application))).all())
        loser = next(app for app in apps if app.status is ApplicationStatus.BLOCKED)
        assert "no_duplicate_application" in loser.policy_result["rules_failed"]


async def test_postgres_preserves_long_canonical_fields_without_truncating(dedup_postgres):
    from app.deduplication.comparison import cities, role_key
    from app.deduplication.service import canonical_fingerprint
    from app.employers.normalization import company_key

    async with dedup_postgres() as session:
        src = source("long-fields")
        session.add(src)
        await session.flush()
        item = job(src.id, "many-cities", "Specialist " + "transport " * 35)
        item.company = "Operator " + "logistics " * 35
        item.cities = [f"Destination {index:03d}" for index in range(80)]
        session.add(item)
        await session.flush()
        assigned = await DeduplicationService().assign(session, item)
        await session.commit()
        canonical = await session.get(CanonicalJob, assigned.canonical_job.id)
        assert canonical is not None
        assert canonical.normalized_location == " ".join(sorted(cities(item)))
        assert len(canonical.normalized_location) > 255
        assert canonical.normalized_company == company_key(item.company)
        assert len(canonical.normalized_company) > 255
        assert canonical.normalized_title == role_key(item.title)
        assert len(canonical.normalized_title) > 255
        assert canonical.canonical_fingerprint == canonical_fingerprint(item)
        split = await DeduplicationService().split(session, item)
        await session.commit()
        assert split.normalized_location == canonical.normalized_location


async def test_postgres_scan_records_failed_insert_and_resumes_committed_cursor(dedup_postgres):
    from app.crawlers.pipeline import ScanService
    from app.models.entities import ScanRun, SourceJob
    from app.models.enums import RunStatus, ScanType
    from tests.integration.test_delucru_pipeline import source_record
    from tests.unit.test_delucru_adapter import FixtureFetcher, finite_category_routes

    source_id = await source_record(dedup_postgres)
    fetcher = FixtureFetcher(finite_category_routes())
    scanner = ScanService(dedup_postgres, build_default_registry(client_factory=lambda _: fetcher))
    async with dedup_postgres() as session:
        await session.execute(
            text(
                "ALTER TABLE source_jobs ADD CONSTRAINT offline_failed_insert "
                "CHECK (external_job_id <> '43867')"
            )
        )
        await session.commit()
    first, _ = await scanner.request_manual_scan(source_id, ScanType.FULL)
    partial = await scanner.run_scan(first.id)
    assert partial.status == RunStatus.PARTIAL
    assert partial.parsing_errors == 1
    assert partial.diagnostics["errors"][-1] == {"external_id": "43867", "type": "IntegrityError"}
    async with dedup_postgres() as session:
        committed_ids = set(await session.scalars(select(SourceJob.external_job_id)))
        assert committed_ids == {"88409", "55318"}
        stored = await session.get(ScanRun, first.id)
        assert "43867" in stored.checkpoint["adapter_state"]["failed_references"]
        assert "43867" not in stored.checkpoint["yielded_external_ids"]
        await session.execute(text("ALTER TABLE source_jobs DROP CONSTRAINT offline_failed_insert"))
        await session.commit()
    resumed, created = await scanner.request_manual_scan(source_id, ScanType.FULL)
    assert created and resumed.diagnostics["resume_parent_scan_id"] == str(first.id)
    completed = await scanner.run_scan(resumed.id)
    assert completed.status == RunStatus.SUCCEEDED
    async with dedup_postgres() as session:
        assert await session.scalar(select(func.count(SourceJob.id))) == 3
    assert fetcher.requested.count("https://www.delucru.md/job/junior-data-scientist-88409") == 1


async def test_postgres_duplicate_reference_commits_progress_before_cancellation(dedup_postgres):
    from app.crawlers.pipeline import ScanService
    from app.models.entities import ScanRun, SourceJob
    from app.models.enums import RunStatus, ScanType
    from tests.integration.test_delucru_pipeline import source_record
    from tests.unit.test_delucru_adapter import BASE, FixtureFetcher, finite_category_routes

    class HeldListingFetcher(FixtureFetcher):
        def __init__(self):
            super().__init__(finite_category_routes())
            self.hold = False
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def get(self, url, **kwargs):
            if self.hold and url == f"{BASE}/jobs/acquisitions?page=2":
                self.entered.set()
                await self.release.wait()
            return await super().get(url, **kwargs)

    source_id = await source_record(dedup_postgres)
    fetcher = HeldListingFetcher()
    scanner = ScanService(dedup_postgres, build_default_registry(client_factory=lambda _: fetcher))
    parent = await scanner.run_scan((await scanner.create_scan(source_id, ScanType.FULL)).id)
    assert parent.status == RunStatus.SUCCEEDED
    async with dedup_postgres() as session:
        saved = await session.get(ScanRun, parent.id)
        saved.status = RunStatus.PARTIAL
        saved.checkpoint = {
            "yielded_external_ids": list(await session.scalars(select(SourceJob.external_job_id))),
            "adapter_state": {
                "scan_entrypoints": [
                    {
                        "url": f"{BASE}/jobs/acquisitions",
                        "category": "offline-retry",
                        "region": None,
                    }
                ]
            },
        }
        await session.commit()
    resumed, _ = await scanner.request_manual_scan(source_id, ScanType.FULL)
    fetcher.hold = True
    task = asyncio.create_task(scanner.run_scan(resumed.id))
    try:
        await asyncio.wait_for(fetcher.entered.wait(), timeout=15)
        # Observe through a separate connection while the worker awaits the next page.
        async with dedup_postgres() as session:
            running = await session.get(ScanRun, resumed.id)
            assert running.checkpoint["page_url"] == f"{BASE}/jobs/acquisitions"
            existing = await session.scalar(
                select(SourceJob).where(SourceJob.external_job_id == "88409")
            )
            assert "offline-retry" in existing.categories_seen
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    interrupted = await scanner.interrupt_scan(resumed.id, reason="runtime_timeout")
    assert interrupted.checkpoint["page_url"] == f"{BASE}/jobs/acquisitions"
    fetcher.hold = False
    retry, _ = await scanner.request_manual_scan(source_id, ScanType.FULL)
    completed = await scanner.run_scan(retry.id)
    assert completed.status == RunStatus.SUCCEEDED and completed.found_jobs == 0
    async with dedup_postgres() as session:
        assert await session.scalar(select(func.count(SourceJob.id))) == 3
    assert fetcher.requested.count(f"{BASE}/job/junior-data-scientist-88409") == 1


async def test_postgres_canonical_text_migration_keeps_rows_and_rejects_lossy_downgrade(
    dedup_postgres,
):
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    path = Path("migrations/versions/c8d2e6f4a901_canonical_normalized_text.py")
    spec = importlib.util.spec_from_file_location("canonical_text_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with dedup_postgres() as session:
        canonical = CanonicalJob(
            normalized_company="offline operator",
            normalized_title="offline courier",
            normalized_location="chisinau",
            canonical_fingerprint="migration-fixture",
        )
        session.add(canonical)
        await session.commit()
        row_id = canonical.id
        connection = await session.connection()

        def apply(sync_connection, function):
            with Operations.context(MigrationContext.configure(sync_connection)):
                function()

        # Start from the existing VARCHAR schema, then exercise the actual migration.
        await connection.run_sync(apply, migration.downgrade)
        await connection.run_sync(apply, migration.upgrade)
        types = await connection.run_sync(
            lambda c: {
                column["name"]: str(column["type"])
                for column in inspect(c).get_columns("canonical_jobs")
            }
        )
        assert all(types[name] == "TEXT" for name in migration.FIELDS)
        await session.refresh(canonical)
        assert canonical.id == row_id and canonical.normalized_location == "chisinau"
        canonical.normalized_location = "destination " * 80
        await session.flush()
        with pytest.raises(RuntimeError, match="without losing stored vacancy data"):
            await connection.run_sync(apply, migration.downgrade)
        await session.commit()
        assert (await session.get(CanonicalJob, row_id)).normalized_location == "destination " * 80
