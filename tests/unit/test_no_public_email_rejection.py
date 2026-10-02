"""A vacancy without a public email cannot be applied to: JobHunter does not use
the job board's internal form. Such an application is rejected by policy instead
of waiting in the owner's review queue, where it could never be approved."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from app.applications import service as application_service
from app.employers import EmployerRelationshipService
from app.employers.relationships import EmployerPolicyOutcome
from app.models.entities import Application, EmployerContact
from app.models.enums import ApplicationStatus, ContactType, MatchDecision, PolicyDecision
from app.policies import PolicyEngine
from tests.unit.test_daily_minimum import additional_candidate
from tests.unit.test_policy_and_email import make_graph, settings
from tests.unit.test_prepare_pending_scope import _wire

NO_EMAIL_REASON = "no_public_email"


def _make_internal(contact: EmployerContact, application: Application) -> None:
    contact.contact_type = ContactType.INTERNAL_JOB_BOARD
    contact.value = "https://jobs.example.com/job-1"
    # prepare() never validates a letter that has no email recipient.
    application.content_validated = False


async def _apply_policy(session, graph, storage: Path):
    _source, profile, preference, resume, _canonical, job, evaluation, contact, application = graph
    return await PolicyEngine(settings(storage)).apply(
        session, application, preference, evaluation, job, resume, contact, profile
    )


async def test_vacancy_without_public_email_is_rejected_not_queued_for_review(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        contact, application = graph[7], graph[8]
        _make_internal(contact, application)

        result = await _apply_policy(session, graph, tmp_path)

        assert "verified_email_contact" in result.rules_failed
        assert result.decision is PolicyDecision.SKIPPED
        assert application.status is ApplicationStatus.CANCELLED
        assert application.policy_decision is PolicyDecision.SKIPPED
        assert application.policy_result["safe_stop_reason"] == NO_EMAIL_REASON
        assert application.policy_result["requires_rematch"] is False


async def test_automatic_no_email_rejection_is_not_recorded_as_an_owner_decision(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        _make_internal(graph[7], graph[8])

        await _apply_policy(session, graph, tmp_path)

        # The owner never looked at it; the learning model must not treat the
        # rejection as the owner's preference.
        assert "owner_rejected" not in graph[8].policy_result


async def test_no_email_vacancy_is_rejected_even_when_the_match_needs_review(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        evaluation, contact, application = graph[6], graph[7], graph[8]
        _make_internal(contact, application)
        evaluation.decision = MatchDecision.PREPARE_FOR_REVIEW
        evaluation.risks = ["salary_unclear"]

        result = await _apply_policy(session, graph, tmp_path)

        assert result.decision is PolicyDecision.SKIPPED
        assert application.status is ApplicationStatus.CANCELLED


async def test_no_email_vacancy_is_rejected_instead_of_waiting_for_an_employer_slot(
    sqlite_session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def busy_employer(self, *args, **kwargs) -> EmployerPolicyOutcome:
        return EmployerPolicyOutcome(
            employer_resolved=True,
            not_suppressed=True,
            no_candidate_withdrawal=True,
            no_active_conversation=True,
            slot_available=False,
            reason="same_employer_application_deferred",
        )

    monkeypatch.setattr(EmployerRelationshipService, "policy_outcome", busy_employer)
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        _make_internal(graph[7], graph[8])

        result = await _apply_policy(session, graph, tmp_path)

        # A deferred application waits for the employer's slot and is then
        # evaluated again; without an email that wait can only end in a reject.
        assert "employer_application_slot_available" in result.rules_failed
        assert result.decision is PolicyDecision.SKIPPED
        assert graph[8].policy_result["safe_stop_reason"] == NO_EMAIL_REASON


async def test_hard_block_still_wins_over_the_no_email_rejection(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        profile, contact, application = graph[1], graph[7], graph[8]
        _make_internal(contact, application)
        profile.confirmed_facts = [{"id": "implicit", "statement": "not confirmed"}]
        application.used_confirmed_facts = ["implicit"]

        result = await _apply_policy(session, graph, tmp_path)

        assert result.decision is PolicyDecision.BLOCKED
        assert application.status is ApplicationStatus.BLOCKED


async def test_email_vacancy_is_still_auto_approved(sqlite_session_factory, tmp_path: Path) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)

        result = await _apply_policy(session, graph, tmp_path)

        assert result.decision is PolicyDecision.AUTO_APPROVED
        assert "safe_stop_reason" not in graph[8].policy_result


async def _legacy_no_email_review_item(session_factory, storage: Path) -> UUID:
    """A review item created before the rule: stuck in the queue, unapprovable."""

    async with session_factory() as session:
        graph = await make_graph(session, storage)
        contact, application = graph[7], graph[8]
        _make_internal(contact, application)
        application.status = ApplicationStatus.PENDING_REVIEW
        application.policy_decision = PolicyDecision.PENDING_REVIEW
        application.policy_result = {
            "rules_failed": ["daily_limit", "letter_validated", "verified_email_contact"]
        }
        await session.commit()
        return application.id


def _count_policy_refreshes(monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    calls: list[UUID] = []
    original = application_service.ApplicationService.reevaluate_policy

    async def counting(self, session, application):
        calls.append(application.id)
        return await original(self, session, application)

    monkeypatch.setattr(application_service.ApplicationService, "reevaluate_policy", counting)
    return calls


async def test_scheduler_clears_existing_no_email_items_from_the_review_queue(
    sqlite_session_factory, tmp_path: Path
) -> None:
    application_id = await _legacy_no_email_review_item(sqlite_session_factory, tmp_path)

    with pytest.MonkeyPatch.context() as patch:
        _wire(patch, sqlite_session_factory, tmp_path)
        refreshed = _count_policy_refreshes(patch)
        await application_service.prepare_pending_applications()

    assert refreshed == [application_id]
    async with sqlite_session_factory() as session:
        application = await session.get(Application, application_id)
        assert application is not None
        assert application.status is ApplicationStatus.CANCELLED
        assert application.policy_decision is PolicyDecision.SKIPPED
        assert application.policy_result["safe_stop_reason"] == NO_EMAIL_REASON


async def test_rejected_no_email_item_is_not_revisited_on_later_cycles(
    sqlite_session_factory, tmp_path: Path
) -> None:
    await _legacy_no_email_review_item(sqlite_session_factory, tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        _wire(patch, sqlite_session_factory, tmp_path)
        await application_service.prepare_pending_applications()

    with pytest.MonkeyPatch.context() as patch:
        _wire(patch, sqlite_session_factory, tmp_path)
        refreshed = _count_policy_refreshes(patch)
        prepared = await application_service.prepare_pending_applications()

    assert refreshed == []
    assert prepared == 0


async def test_owner_rejected_no_email_item_keeps_the_owner_decision(
    sqlite_session_factory, tmp_path: Path
) -> None:
    application_id = await _legacy_no_email_review_item(sqlite_session_factory, tmp_path)
    async with sqlite_session_factory() as session:
        application = await session.get(Application, application_id)
        assert application is not None
        application.policy_result = {**application.policy_result, "owner_rejected": True}
        await session.commit()

    with pytest.MonkeyPatch.context() as patch:
        _wire(patch, sqlite_session_factory, tmp_path)
        refreshed = _count_policy_refreshes(patch)
        await application_service.prepare_pending_applications()

    assert refreshed == []
    async with sqlite_session_factory() as session:
        application = await session.get(Application, application_id)
        assert application is not None
        assert application.policy_result["owner_rejected"] is True
        assert "safe_stop_reason" not in application.policy_result


@pytest.mark.parametrize("clock", [1790933400.0, 1790933700.0])
async def test_no_email_backlog_is_cleared_in_one_cycle_whatever_the_refresh_rotation(
    sqlite_session_factory, tmp_path: Path, clock: float
) -> None:
    """The transient-rule refresh works through a rotating batch; the owner
    should not wait for the rotation to reach an item they cannot approve."""
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        contact, no_email = graph[7], graph[8]
        _make_internal(contact, no_email)
        no_email.status = ApplicationStatus.PENDING_REVIEW
        no_email.policy_decision = PolicyDecision.PENDING_REVIEW
        no_email.policy_result = {
            "rules_failed": ["daily_limit", "letter_validated", "verified_email_contact"]
        }
        for company in ("Second Company", "Third Company"):
            other = (await additional_candidate(session, graph, company=company, score=95))[4]
            other.status = ApplicationStatus.PENDING_REVIEW
            other.policy_decision = PolicyDecision.PENDING_REVIEW
            other.policy_result = {"rules_failed": ["daily_limit"]}
        await session.commit()
        no_email_id = no_email.id

    with pytest.MonkeyPatch.context() as patch:
        current_settings = settings(tmp_path).model_copy(
            update={"application_policy_refresh_batch_size": 1}
        )
        patch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
        patch.setattr(application_service, "get_settings", lambda: current_settings)
        patch.setattr(application_service.time, "time", lambda: clock)
        await application_service.prepare_pending_applications()

    async with sqlite_session_factory() as session:
        application = await session.get(Application, no_email_id)
        assert application is not None
        assert application.status is ApplicationStatus.CANCELLED
        assert application.policy_result["safe_stop_reason"] == NO_EMAIL_REASON
