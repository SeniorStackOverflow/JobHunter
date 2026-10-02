"""The policy approves no more applications than can still be sent today.

Approving beyond the day's capacity left a pile of approved applications that
could not go out until tomorrow; every policy evaluation re-validated the whole
pile, which made the scheduler cycle several times slower. The overflow waits
as deferred, outside the owner's review queue, and is approved on a later day."""

from __future__ import annotations

from pathlib import Path

# app.policies cannot be the first application package imported: it and
# app.applications import each other.
import app.applications  # noqa: F401
from app.models.enums import ApplicationStatus, PolicyDecision
from app.policies import PolicyEngine
from tests.unit.test_daily_minimum import additional_candidate, apply_candidate
from tests.unit.test_policy_and_email import make_graph, settings

DAILY_LIMIT_REASON = "daily_limit_reached"


async def _three_candidates(session, storage: Path):
    """The fixture profile may send two applications a day; three vacancies fit."""

    graph = await make_graph(session, storage)
    first = (graph[4], graph[5], graph[6], graph[7], graph[8])
    second = await additional_candidate(
        session, graph, company="Second Company", score=95, decision=graph[6].decision
    )
    third = await additional_candidate(
        session, graph, company="Third Company", score=95, decision=graph[6].decision
    )
    return graph, first, second, third


async def test_policy_approves_only_what_today_can_still_send(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph, first, second, third = await _three_candidates(session, tmp_path)
        engine = PolicyEngine(settings(tmp_path))

        decisions = [
            (await apply_candidate(session, engine, graph, candidate)).decision
            for candidate in (first, second, third)
        ]

        assert decisions == [
            PolicyDecision.AUTO_APPROVED,
            PolicyDecision.AUTO_APPROVED,
            PolicyDecision.DEFERRED,
        ]


async def test_overflow_waits_as_deferred_not_in_the_review_queue(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph, first, second, third = await _three_candidates(session, tmp_path)
        engine = PolicyEngine(settings(tmp_path))
        for candidate in (first, second, third):
            result = await apply_candidate(session, engine, graph, candidate)

        overflow = third[4]
        assert result.rules_failed == ["daily_limit"]
        assert overflow.status is ApplicationStatus.DEFERRED
        assert overflow.policy_result["safe_stop_reason"] == DAILY_LIMIT_REASON
        assert overflow.policy_result["requires_rematch"] is False


async def test_overflow_is_approved_once_capacity_is_available(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph, first, second, third = await _three_candidates(session, tmp_path)
        engine = PolicyEngine(settings(tmp_path))
        for candidate in (first, second, third):
            await apply_candidate(session, engine, graph, candidate)

        graph[2].maximum_daily_applications = 3
        result = await apply_candidate(session, engine, graph, third)

        assert result.decision is PolicyDecision.AUTO_APPROVED
        assert "safe_stop_reason" not in third[4].policy_result


async def test_an_approved_application_is_not_blocked_by_the_other_reservations(
    sqlite_session_factory, tmp_path: Path
) -> None:
    """The sender re-evaluates each approved application right before sending;
    the reservations of the others must not turn that check into a deadlock."""
    async with sqlite_session_factory() as session:
        graph, first, second, _third = await _three_candidates(session, tmp_path)
        engine = PolicyEngine(settings(tmp_path))
        for candidate in (first, second):
            await apply_candidate(session, engine, graph, candidate)

        result = await apply_candidate(session, engine, graph, first)

        assert result.decision is PolicyDecision.AUTO_APPROVED


def test_panel_explains_a_daily_limit_deferral() -> None:
    from app.ui.presentation import _application_deferred_note

    application = {
        "status": "deferred",
        "policy_result": {"safe_stop_reason": DAILY_LIMIT_REASON, "rules_failed": ["daily_limit"]},
    }

    assert _application_deferred_note(application) == (
        "Дневной лимит отправок исчерпан — отклик будет одобрен автоматически, "
        "когда появится место."
    )


def test_panel_keeps_the_employer_explanation_for_other_deferrals() -> None:
    from app.ui.presentation import _application_deferred_note

    application = {
        "status": "deferred",
        "policy_result": {
            "safe_stop_reason": "same_employer_application_deferred",
            "rules_failed": ["employer_application_slot_available"],
        },
    }

    assert _application_deferred_note(application) is None


def test_application_detail_template_uses_the_deferred_note() -> None:
    template = Path("app/admin/templates/application_detail.html").read_text(encoding="utf-8")

    assert "application_deferred_note(application)" in template


async def test_minimum_audit_counts_a_capacity_deferral_as_ready(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from app.applications.diagnostics import daily_minimum_audit

    async with sqlite_session_factory() as session:
        graph, first, second, third = await _three_candidates(session, tmp_path)
        engine = PolicyEngine(settings(tmp_path))
        for candidate in (first, second, third):
            await apply_candidate(session, engine, graph, candidate)

        audit = await daily_minimum_audit(session, graph[1].id)

        # Two approved reservations and the one that only waits for capacity.
        assert audit["primary_blockers"]["ready"] == 3
        assert audit["primary_blockers"]["hard_safety"] == 0
        assert audit["primary_blockers"]["employer"] == 0


async def test_minimum_audit_names_the_real_blocker_when_the_limit_is_also_reached(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from app.applications.diagnostics import daily_minimum_audit

    async with sqlite_session_factory() as session:
        graph, first, second, third = await _three_candidates(session, tmp_path)
        engine = PolicyEngine(settings(tmp_path))
        for candidate in (first, second):
            await apply_candidate(session, engine, graph, candidate)
        third[1].category = "finance"  # not an auto-send category of the profile
        result = await apply_candidate(session, engine, graph, third)
        assert set(result.rules_failed) == {"category_allowed_for_auto_send", "daily_limit"}

        audit = await daily_minimum_audit(session, graph[1].id)

        assert audit["primary_blockers"]["category"] == 1
        assert audit["primary_blockers"]["hard_safety"] == 0
