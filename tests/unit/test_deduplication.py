from __future__ import annotations

import pytest

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


@pytest.mark.parametrize(
    "scenario",
    [
        "legal_suffix",
        "translated_title",
        "city_alias",
        "secondary_email",
        "phone_only",
        "translated_experience",
        "translated_schedule",
    ],
)
async def test_cross_board_variants_merge_from_all_evidence(
    sqlite_session_factory, scenario: str
) -> None:
    async with sqlite_session_factory() as session:
        sources = [source("one"), source("two")]
        session.add_all(sources)
        await session.flush()
        first, second = [
            job(src.id, key, "Warehouse Operator")
            for src, key in zip(sources, ("a", "b"), strict=True)
        ]
        first.employer_url = "https://www.delucru.md/company/logico-42"
        second.employer_url = "https://www.rabota.md/ru/companies/logico"
        if scenario == "legal_suffix":
            second.company = "LogiCo SRL"
        elif scenario == "translated_title":
            first.title, second.title = "Agent vânzări", "Менеджер по продажам"
        elif scenario == "city_alias":
            first.location, first.cities = "Chișinău", ["Chișinău"]
            second.location, second.cities = "Кишинёв", ["Кишинёв"]
        elif scenario == "secondary_email":
            first.public_email, second.public_email = "main@first.test", "main@second.test"
            first.public_emails = ["main@first.test", "careers@logico.example"]
            second.public_emails = ["main@second.test", "careers@logico.example"]
        elif scenario == "phone_only":
            first.public_email = second.public_email = None
            first.public_phone, second.public_phone = "+37360004589", "060 004 589"
        elif scenario == "translated_experience":
            first.required_experience, second.required_experience = "fără experiență", "без опыта"
        elif scenario == "translated_schedule":
            first.schedule, second.schedule = "Full-time", "полный рабочий день"
        session.add_all([first, second])
        await session.flush()
        service = DeduplicationService()
        a, b = await service.assign(session, first), await service.assign(session, second)
        assert a.canonical_job.id == b.canonical_job.id
        assert first.employer_id == second.employer_id
        assert second.raw_metadata["deduplication"]["status"] == "assigned"


@pytest.mark.parametrize(
    "field, first_value, second_value",
    [
        ("schedule", "day", "night"),
        ("description", "Day shift only", "Night shift only"),
        ("requirements", "Mandatory driving licence", "Mandatory forklift certificate"),
        ("cities", ["Chisinau"], ["Balti"]),
        ("workplace_type", "remote", "onsite"),
    ],
)
async def test_same_short_fingerprint_does_not_merge_different_conditions(
    sqlite_session_factory, field, first_value, second_value
) -> None:
    async with sqlite_session_factory() as session:
        src = source("one")
        session.add(src)
        await session.flush()
        first, second = (
            job(src.id, "a", "Warehouse Operator"),
            job(src.id, "b", "Warehouse Operator"),
        )
        setattr(first, field, first_value)
        setattr(second, field, second_value)
        session.add_all([first, second])
        await session.flush()
        service = DeduplicationService()
        assert (await service.assign(session, first)).canonical_job.id != (
            await service.assign(session, second)
        ).canonical_job.id


async def test_board_domain_and_shared_recruiter_do_not_merge_distinct_clients(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        src = source("one")
        session.add(src)
        await session.flush()
        service = DeduplicationService()
        for contacts in (False, True):
            first = job(src.id, f"a-{contacts}", "Warehouse Operator")
            second = job(src.id, f"b-{contacts}", "Warehouse Operator")
            first.employer_url = "https://www.delucru.md/company/client-one"
            second.employer_url = "https://www.delucru.md/company/client-two"
            first.public_email = second.public_email = "recruiter@gmail.com" if contacts else None
            if contacts:
                first.company, second.company = "Client One", "Client Two"
                first.public_phone = second.public_phone = "+37360004589"
            session.add_all([first, second])
            await session.flush()
            assert (await service.assign(session, first)).canonical_job.id != (
                await service.assign(session, second)
            ).canonical_job.id


async def test_insufficient_content_is_saved_for_review_and_not_blindly_merged(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        src = source("one")
        session.add(src)
        await session.flush()
        first, second = (
            job(src.id, "a", "Warehouse Operator"),
            job(src.id, "b", "Warehouse Operator"),
        )
        first.description = second.description = None
        session.add_all([first, second])
        await session.flush()
        service = DeduplicationService()
        a, b = await service.assign(session, first), await service.assign(session, second)
        assert a.canonical_job.id != b.canonical_job.id
        assert second.raw_metadata["deduplication"]["status"] == "needs_review"
        assert second.raw_metadata["deduplication"]["candidate_canonical_ids"] == [
            str(a.canonical_job.id)
        ]


async def test_new_contacts_reassess_existing_assignment_without_silent_employer_merge(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        src = source("one")
        session.add(src)
        await session.flush()
        first, second = (
            job(src.id, "a", "Warehouse Operator"),
            job(src.id, "b", "Warehouse Operator"),
        )
        first.public_email = second.public_email = None
        session.add_all([first, second])
        await session.flush()
        service = DeduplicationService()
        await service.assign(session, first)
        await service.assign(session, second)
        original = second.canonical_job_id
        first.public_email = "careers@logico.example"
        await service.assign(session, first)
        second.public_email = "careers@logico.example"
        await service.assign(session, second)
        assert second.canonical_job_id == original
        assert second.raw_metadata["deduplication"]["status"] == "needs_review"
        assert second.raw_metadata["deduplication_history"]


@pytest.mark.parametrize("translated", [False, True])
async def test_unknown_translated_roles_require_review_but_known_different_roles_stay_separate(
    sqlite_session_factory, translated
) -> None:
    async with sqlite_session_factory() as session:
        src = source("one")
        session.add(src)
        await session.flush()
        titles = ("Arhitect", "Архитектор") if translated else ("Cook", "Cashier")
        first, second = job(src.id, "a", titles[0]), job(src.id, "b", titles[1])
        session.add_all([first, second])
        await session.flush()
        service = DeduplicationService()
        await service.assign(session, first)
        await service.assign(session, second)
        assert first.canonical_job_id != second.canonical_job_id
        assert (second.raw_metadata["deduplication"]["status"] == "needs_review") is translated


async def test_repeated_split_is_idempotent_and_repairs_previous_primary(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        src = source("one")
        session.add(src)
        await session.flush()
        first, second = (
            job(src.id, "a", "Warehouse Operator"),
            job(src.id, "b", "Warehouse Operator"),
        )
        session.add_all([first, second])
        await session.flush()
        service = DeduplicationService()
        previous = (await service.assign(session, first)).canonical_job
        await service.assign(session, second)
        split = await service.split(session, first)
        assert previous.primary_source_job_id == second.id
        assert (await service.split(session, first)).id == split.id
