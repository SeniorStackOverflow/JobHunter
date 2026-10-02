"""Category choices belong to a (profile, source) pair.

Every adapter has its own category vocabulary, so one global list of free-text
slugs cannot describe several sources. The owner picks from the categories the
source itself publishes."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.matching.bindings import preference_fingerprint
from app.models.entities import (
    JobPreference,
    JobSource,
    MatchEvaluation,
    ProfileSourcePreference,
    SourceCategory,
    UserProfile,
)
from app.models.enums import ProfileStatus, SourceHealth
from app.profiles.source_categories import (
    SourceCategoryPolicy,
    category_policy,
    crawl_category_slugs,
    known_source_categories,
    set_source_categories,
)
from tests.unit.test_daily_minimum import additional_candidate
from tests.unit.test_policy_and_email import make_graph


async def _catalog(session, source: JobSource, *items: tuple[str, str, str]) -> None:
    for external_id, name, locale in items:
        session.add(
            SourceCategory(
                source_id=source.id,
                external_id=external_id,
                name=name,
                url=f"{source.base_url}/{locale}/category/{external_id}",
                locale=locale,
            )
        )
    await session.flush()


async def _graph_with_catalog(session, storage: Path):
    graph = await make_graph(session, storage)
    await _catalog(
        session,
        graph[0],
        ("technology", "IT, Программирование", "ru"),
        ("technology", "IT, Programare", "ro"),
        ("warehouses", "Складское хозяйство", "ru"),
        ("workers", "Разнорабочие, грузчики", "ru"),
        ("calls", "Работа на телефоне", "ru"),
    )
    return graph


async def test_unconfigured_source_uses_the_profile_wide_lists(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        source, preference = graph[0], graph[2]
        preference.forbidden_categories = ["calls"]

        policy = await category_policy(session, preference, source.id)

        assert policy == SourceCategoryPolicy(
            search=("technology",),
            auto_send=("technology",),
            excluded=("calls",),
            configured=False,
        )


async def test_saved_choice_is_scoped_to_its_source(sqlite_session_factory, tmp_path: Path) -> None:
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        source, profile, preference = graph[0], graph[1], graph[2]
        other = JobSource(
            name="Other board",
            base_url="https://other.example.com",
            adapter_type="fixture_source",
            configuration={},
            health_status=SourceHealth.HEALTHY,
        )
        session.add(other)
        await session.flush()
        await _catalog(session, other, ("depozit", "Depozit", "ro"))

        await set_source_categories(
            session,
            profile_id=profile.id,
            source_id=source.id,
            search=["warehouses", "workers"],
            auto_send=["warehouses"],
            excluded=["calls"],
        )
        await set_source_categories(
            session,
            profile_id=profile.id,
            source_id=other.id,
            search=["depozit"],
            auto_send=[],
            excluded=[],
        )

        first = await category_policy(session, preference, source.id)
        second = await category_policy(session, preference, other.id)
        assert first == SourceCategoryPolicy(
            search=("warehouses", "workers"),
            auto_send=("warehouses",),
            excluded=("calls",),
            configured=True,
        )
        assert second == SourceCategoryPolicy(
            search=("depozit",), auto_send=(), excluded=(), configured=True
        )


async def test_a_category_the_source_does_not_publish_is_rejected(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)

        with pytest.raises(ValueError, match="unknown source category: sklad"):
            await set_source_categories(
                session,
                profile_id=graph[1].id,
                source_id=graph[0].id,
                search=["sklad"],
                auto_send=[],
                excluded=[],
            )


async def test_automatic_sending_implies_search_and_exclusion_wins(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)

        await set_source_categories(
            session,
            profile_id=graph[1].id,
            source_id=graph[0].id,
            search=["calls"],
            auto_send=["warehouses", "calls"],
            excluded=["calls"],
        )

        policy = await category_policy(session, graph[2], graph[0].id)
        assert policy.search == ("warehouses",)
        assert policy.auto_send == ("warehouses",)
        assert policy.excluded == ("calls",)


async def test_profile_wide_lists_stay_the_default_for_unconfigured_sources(
    sqlite_session_factory, tmp_path: Path
) -> None:
    """They are part of the evaluation fingerprint: rewriting them on every
    per-source change would re-send every vacancy of the profile to the model."""
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        source, profile, preference = graph[0], graph[1], graph[2]
        before = preference_fingerprint(preference)

        await set_source_categories(
            session,
            profile_id=profile.id,
            source_id=source.id,
            search=["workers", "warehouses"],
            auto_send=["warehouses"],
            excluded=["calls"],
        )

        assert preference.allowed_categories == ["technology"]
        assert preference.auto_send_categories == ["technology"]
        assert preference_fingerprint(preference) == before


async def _second_vacancy(session, graph, *, category: str):
    job, evaluation = (await additional_candidate(session, graph, company="Second", score=90))[1:3]
    job.category = category
    job.categories_seen = [category]
    evaluation.preference_fingerprint = preference_fingerprint(graph[2])
    await session.flush()
    return evaluation


async def test_adding_a_category_reevaluates_only_vacancies_of_that_category(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        source, profile, preference, technology = graph[0], graph[1], graph[2], graph[6]
        workers = await _second_vacancy(session, graph, category="workers")
        current = preference_fingerprint(preference)
        assert technology.preference_fingerprint == current

        await set_source_categories(
            session,
            profile_id=profile.id,
            source_id=source.id,
            search=["technology", "workers"],
            auto_send=["technology"],
            excluded=[],
        )

        # Unset fingerprint = stale: the matcher picks the vacancy up again.
        assert workers.preference_fingerprint is None
        assert technology.preference_fingerprint == current


async def test_removing_or_excluding_a_category_reevaluates_its_vacancies(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        source, profile, technology = graph[0], graph[1], graph[6]
        workers = await _second_vacancy(session, graph, category="workers")
        await set_source_categories(
            session,
            profile_id=profile.id,
            source_id=source.id,
            search=["technology", "workers"],
            auto_send=[],
            excluded=[],
        )
        workers.preference_fingerprint = technology.preference_fingerprint
        await session.flush()

        await set_source_categories(
            session,
            profile_id=profile.id,
            source_id=source.id,
            search=["workers"],
            auto_send=[],
            excluded=["technology"],
        )

        assert technology.preference_fingerprint is None
        assert workers.preference_fingerprint is not None


async def test_changing_only_automatic_sending_reevaluates_nothing(
    sqlite_session_factory, tmp_path: Path
) -> None:
    """Auto-send is a delivery rule the policy rechecks; it is not model input."""
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        source, profile, technology = graph[0], graph[1], graph[6]
        before = technology.preference_fingerprint

        await set_source_categories(
            session,
            profile_id=profile.id,
            source_id=source.id,
            search=["technology"],
            auto_send=[],
            excluded=[],
        )

        assert technology.preference_fingerprint == before


async def test_another_profiles_evaluations_are_not_touched(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        source, profile, technology = graph[0], graph[1], graph[6]
        other = UserProfile(name="Other")
        session.add(other)
        await session.flush()
        session.add(JobPreference(profile_id=other.id, allowed_categories=["technology"]))
        values = {
            column.key: getattr(technology, column.key)
            for column in MatchEvaluation.__table__.columns
            if column.key != "id"
        }
        foreign = MatchEvaluation(**{**values, "profile_id": other.id})
        session.add(foreign)
        await session.flush()

        await set_source_categories(
            session,
            profile_id=profile.id,
            source_id=source.id,
            search=["workers"],
            auto_send=[],
            excluded=[],
        )

        assert technology.preference_fingerprint is None
        assert foreign.preference_fingerprint is not None


async def test_another_accounts_profile_cannot_be_changed(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from uuid import uuid4

    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)

        with pytest.raises(LookupError):
            await set_source_categories(
                session,
                profile_id=graph[1].id,
                source_id=graph[0].id,
                search=["workers"],
                auto_send=[],
                excluded=[],
                owner_account_id=uuid4(),
            )
        assert (await session.scalars(select(ProfileSourcePreference))).all() == []


async def test_known_categories_are_named_in_the_preferred_language(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        hidden = await session.scalar(
            select(SourceCategory).where(SourceCategory.external_id == "workers")
        )
        assert hidden is not None
        hidden.active = False
        await session.flush()

        options = await known_source_categories(session, graph[0].id)

        assert [(item.external_id, item.name) for item in options] == [
            ("technology", "IT, Программирование"),
            ("calls", "Работа на телефоне"),
            ("warehouses", "Складское хозяйство"),
        ]


async def test_crawl_scope_is_the_union_of_processing_profiles(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        source, profile = graph[0], graph[1]
        draft = UserProfile(name="Draft", status=ProfileStatus.DRAFT)
        session.add(draft)
        await session.flush()
        session.add(JobPreference(profile_id=draft.id))
        await session.flush()
        await set_source_categories(
            session,
            profile_id=profile.id,
            source_id=source.id,
            search=["warehouses", "workers"],
            auto_send=[],
            excluded=[],
        )
        await set_source_categories(
            session,
            profile_id=draft.id,
            source_id=source.id,
            search=["calls"],
            auto_send=[],
            excluded=[],
        )

        # A profile that is not processed must not widen what is crawled.
        assert await crawl_category_slugs(session, source.id) == ["warehouses", "workers"]


async def test_crawl_scope_is_empty_until_someone_chooses_categories(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)

        assert await crawl_category_slugs(session, graph[0].id) == []


async def test_a_source_excluded_by_the_profile_does_not_widen_the_crawl(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from app.profiles.sources import set_source_selected

    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        source, profile = graph[0], graph[1]
        await set_source_categories(
            session,
            profile_id=profile.id,
            source_id=source.id,
            search=["workers"],
            auto_send=[],
            excluded=[],
        )
        await set_source_selected(
            session, profile_id=profile.id, source_id=source.id, enabled=False
        )

        assert await crawl_category_slugs(session, source.id) == []
        # Excluding a source keeps the category choice for when it is enabled again.
        assert (await category_policy(session, graph[2], source.id)).search == ("workers",)


class _ScopedAdapter:
    def __init__(self) -> None:
        self.scope: list[str] | None = None

    def set_incremental_categories(self, slugs: list[str]) -> None:
        self.scope = list(slugs)


async def test_scan_uses_the_categories_profiles_chose_for_the_source(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from app.crawlers.pipeline import apply_profile_category_scope

    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        await set_source_categories(
            session,
            profile_id=graph[1].id,
            source_id=graph[0].id,
            search=["workers", "warehouses"],
            auto_send=[],
            excluded=[],
        )
        adapter = _ScopedAdapter()

        await apply_profile_category_scope(session, graph[0], adapter)

        assert adapter.scope == ["warehouses", "workers"]


async def test_scan_keeps_the_source_default_until_categories_are_chosen(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from app.crawlers.pipeline import apply_profile_category_scope

    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        adapter = _ScopedAdapter()

        await apply_profile_category_scope(session, graph[0], adapter)

        assert adapter.scope is None


async def test_an_adapter_without_categories_is_left_alone(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from app.crawlers.pipeline import apply_profile_category_scope

    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        await set_source_categories(
            session,
            profile_id=graph[1].id,
            source_id=graph[0].id,
            search=["workers"],
            auto_send=[],
            excluded=[],
        )

        await apply_profile_category_scope(session, graph[0], object())


def test_rabota_adapter_narrows_incremental_scans_to_the_given_categories() -> None:
    from app.crawlers.adapters.rabota_md import RabotaMdAdapter
    from app.crawlers.adapters.rabota_md.adapter import RabotaMdConfig
    from tests.unit.test_rabota_adapter import FixtureFetcher

    adapter = RabotaMdAdapter(RabotaMdConfig(), http_fetcher=FixtureFetcher())

    adapter.set_incremental_categories(["workers", "warehouses"])

    assert adapter.config.incremental_category_slugs == ["workers", "warehouses"]


async def test_picker_lists_every_known_category_with_its_state(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from app.profiles.source_categories import source_category_picker

    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        await set_source_categories(
            session,
            profile_id=graph[1].id,
            source_id=graph[0].id,
            search=["workers", "warehouses"],
            auto_send=["warehouses"],
            excluded=["calls"],
        )

        picker = await source_category_picker(session, graph[2], graph[0].id)

        # Chosen categories come first, then the rest by name.
        assert [(item.external_id, item.name, item.state) for item in picker.choices] == [
            ("warehouses", "Складское хозяйство", "auto"),
            ("workers", "Разнорабочие, грузчики", "search"),
            ("calls", "Работа на телефоне", "excluded"),
            ("technology", "IT, Программирование", "off"),
        ]
        assert (picker.searched, picker.auto_sent, picker.excluded) == (2, 1, 1)
        assert picker.configured is True
        assert picker.unknown == []


async def test_picker_shows_the_profile_wide_lists_until_the_source_is_configured(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from app.profiles.source_categories import source_category_picker

    async with sqlite_session_factory() as session:
        graph = await _graph_with_catalog(session, tmp_path)
        graph[2].allowed_categories = ["technology", "legacy-free-text"]

        picker = await source_category_picker(session, graph[2], graph[0].id)

        states = {item.external_id: item.state for item in picker.choices}
        assert states["technology"] == "auto"
        assert picker.configured is False
        # A value typed by hand that the source does not publish is reported, not hidden.
        assert picker.unknown == ["legacy-free-text"]


def test_form_states_become_the_three_lists() -> None:
    from app.profiles.source_categories import category_lists_from_states

    lists = category_lists_from_states(
        {
            "category:workers": "search",
            "category:warehouses": "auto",
            "category:calls": "excluded",
            "category:it": "off",
            "csrf_token": "x",
        }
    )

    assert lists == (["workers"], ["warehouses"], ["calls"])


def test_an_unknown_form_state_is_rejected() -> None:
    from app.profiles.source_categories import category_lists_from_states

    with pytest.raises(ValueError, match="unknown category state"):
        category_lists_from_states({"category:workers": "maybe"})
