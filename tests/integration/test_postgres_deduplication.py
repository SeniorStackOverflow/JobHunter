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
