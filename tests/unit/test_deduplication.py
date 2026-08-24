from __future__ import annotations

from app.deduplication import DeduplicationService
from app.models.entities import JobSource, SourceJob


def source(name: str) -> JobSource:
    return JobSource(
        name=name,
        base_url=f"https://{name}.example.com",
        adapter_type="fixture_source",
        configuration={},
    )


def job(source_id, external_id: str, title: str) -> SourceJob:
    return SourceJob(
        source_id=source_id,
        external_job_id=external_id,
        canonical_url=f"https://jobs.example.com/{external_id}",
        localized_urls={},
        title=title,
        company="LogiCo",
        categories_seen=["warehouse"],
        category="warehouse",
        description="Operate scanners and sort goods.",
        cities=["Balti"],
        location="Balti",
        public_email="careers@logico.example",
        content_hash=f"hash-{external_id}",
        matching_content_hash=f"matching-{external_id}",
        source_fingerprint=f"source-{external_id}",
        raw_metadata={},
    )


async def test_cross_source_merge_is_reversible(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        first_source = source("one")
        second_source = source("two")
        session.add_all([first_source, second_source])
        await session.flush()
        first = job(first_source.id, "a", "Warehouse Assistant")
        second = job(second_source.id, "b", "Warehouse Assistant")
        session.add_all([first, second])
        await session.flush()
        service = DeduplicationService()
        first_result = await service.assign(session, first)
        second_result = await service.assign(session, second)
        assert first_result.canonical_job.id == second_result.canonical_job.id
        split = await service.split(session, second)
        assert second.canonical_job_id == split.id
        assert first.canonical_job_id != second.canonical_job_id


async def test_similar_but_distinct_roles_do_not_merge(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        first_source = source("one")
        second_source = source("two")
        session.add_all([first_source, second_source])
        await session.flush()
        day = job(first_source.id, "day", "Day Warehouse Operator")
        night = job(second_source.id, "night", "Night Warehouse Operator")
        session.add_all([day, night])
        await session.flush()
        service = DeduplicationService()
        day_result = await service.assign(session, day)
        night_result = await service.assign(session, night)
        assert day_result.canonical_job.id != night_result.canonical_job.id
