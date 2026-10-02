"""Filtering, model input, auto-send policy and the daily target follow the
category choice of the vacancy's own source."""

from __future__ import annotations

from pathlib import Path

from app.applications.daily_target import daily_target_state
from app.matching.prefilter import DeterministicPrefilter
from app.matching.service import build_match_request
from app.models.entities import JobSource, SourceCategory
from app.models.enums import ApplicationStatus, PolicyDecision, SourceHealth
from app.policies import PolicyEngine
from app.profiles.source_categories import SourceCategoryPolicy, set_source_categories
from tests.unit.test_daily_minimum import apply_candidate
from tests.unit.test_policy_and_email import make_graph, settings


def _policy(**lists: tuple[str, ...]) -> SourceCategoryPolicy:
    return SourceCategoryPolicy(
        search=lists.get("search", ()),
        auto_send=lists.get("auto_send", ()),
        excluded=lists.get("excluded", ()),
        configured=True,
    )


async def test_prefilter_uses_the_source_choice_instead_of_the_profile_wide_list(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        profile, preference, job = graph[1], graph[2], graph[5]
        job.category = "workers"
        job.categories_seen = ["workers"]
        prefilter = DeterministicPrefilter()

        legacy = prefilter.evaluate(job, preference, profile, resume_fit=80)
        scoped = prefilter.evaluate(
            job, preference, profile, resume_fit=80, category_policy=_policy(search=("workers",))
        )

        assert "category_not_allowed" in " ".join(legacy.reasons)
        assert "category_allowed" in scoped.requirements_met
        assert scoped.eligible_for_ai is True


async def test_prefilter_skips_a_category_excluded_for_the_source(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        profile, preference, job = graph[1], graph[2], graph[5]
        job.categories_seen = ["technology", "calls"]

        result = DeterministicPrefilter().evaluate(
            job,
            preference,
            profile,
            resume_fit=80,
            category_policy=_policy(search=("technology",), excluded=("calls",)),
        )

        assert result.eligible_for_ai is False
        assert "category_forbidden" in " ".join(result.reasons)


async def test_model_input_lists_the_source_choice(sqlite_session_factory, tmp_path: Path) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        profile, preference, job = graph[1], graph[2], graph[5]
        policy = _policy(search=("technology", "workers"), excluded=("calls",))
        deterministic = DeterministicPrefilter().evaluate(
            job, preference, profile, resume_fit=80, category_policy=policy
        )

        request = build_match_request(
            job, profile, preference, prefilter=deterministic, category_policy=policy
        )

        assert request.preference_context["allowed_categories"] == ["technology", "workers"]
        assert request.preference_context["forbidden_categories"] == ["calls"]


async def _choose(session, graph, *, search: list[str], auto_send: list[str]) -> None:
    source = graph[0]
    for external_id in {"technology", "workers", *search, *auto_send}:
        session.add(
            SourceCategory(
                source_id=source.id,
                external_id=external_id,
                name=external_id.title(),
                url=f"{source.base_url}/{external_id}",
                locale="ru",
            )
        )
    await session.flush()
    await set_source_categories(
        session,
        profile_id=graph[1].id,
        source_id=source.id,
        search=search,
        auto_send=auto_send,
        excluded=[],
    )


async def _another_source_auto_sends_technology(session, graph) -> None:
    """The profile also uses a second board where "technology" is auto-sent, so
    the profile-wide mirror contains it; the first source must not inherit that."""

    other = JobSource(
        name="Other board",
        base_url="https://other.example.com",
        adapter_type="fixture_source",
        configuration={},
        health_status=SourceHealth.HEALTHY,
    )
    session.add(other)
    await session.flush()
    session.add(
        SourceCategory(
            source_id=other.id,
            external_id="technology",
            name="Technology",
            url="https://other.example.com/technology",
            locale="ru",
        )
    )
    await session.flush()
    await set_source_categories(
        session,
        profile_id=graph[1].id,
        source_id=other.id,
        search=["technology"],
        auto_send=["technology"],
        excluded=[],
    )


async def test_policy_auto_sends_only_categories_chosen_for_the_source(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        candidate = (graph[4], graph[5], graph[6], graph[7], graph[8])
        engine = PolicyEngine(settings(tmp_path))
        await _choose(session, graph, search=["technology", "workers"], auto_send=["workers"])
        await _another_source_auto_sends_technology(session, graph)
        assert "technology" in graph[2].auto_send_categories

        blocked = await apply_candidate(session, engine, graph, candidate)
        await _choose_again(session, graph, auto_send=["technology", "workers"])
        allowed = await apply_candidate(session, engine, graph, candidate)

        assert "category_allowed_for_auto_send" in blocked.rules_failed
        assert blocked.decision is PolicyDecision.PENDING_REVIEW
        assert allowed.decision is PolicyDecision.AUTO_APPROVED


async def _choose_again(session, graph, *, auto_send: list[str]) -> None:
    await set_source_categories(
        session,
        profile_id=graph[1].id,
        source_id=graph[0].id,
        search=["technology", "workers"],
        auto_send=auto_send,
        excluded=[],
    )


async def test_daily_target_reserves_by_the_source_choice(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        preference = graph[2]
        candidate = (graph[4], graph[5], graph[6], graph[7], graph[8])
        engine = PolicyEngine(settings(tmp_path))
        await _choose(session, graph, search=["technology"], auto_send=["technology"])
        result = await apply_candidate(session, engine, graph, candidate)
        assert result.decision is PolicyDecision.AUTO_APPROVED
        assert graph[8].status is ApplicationStatus.AUTO_APPROVED

        reserved = (await daily_target_state(session, preference)).reserved
        # Same search choice (the evaluation stays current); only auto-send changes.
        await set_source_categories(
            session,
            profile_id=graph[1].id,
            source_id=graph[0].id,
            search=["technology"],
            auto_send=[],
            excluded=[],
        )
        await _another_source_auto_sends_technology(session, graph)
        assert preference.auto_send_categories == ["technology"]
        after_change = (await daily_target_state(session, preference)).reserved

        assert reserved == 1
        # The approval no longer matches the source's auto-send choice.
        assert after_change == 0
