"""An employer who has not answered an application is not frozen forever.

Replies arrive within days; after the configured window without one, the
employer may receive an application for another vacancy. A reply, an interview
or a suppression still freezes the employer, and the window restarts with every
new send."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.applications import service as application_service
from app.applications.diagnostics import daily_minimum_audit
from app.employers import EmployerRelationshipService
from app.models.entities import Application, JobSource
from app.models.enums import (
    ApplicationStatus,
    EmployerInteractionChannel,
    EmployerInteractionType,
    PolicyDecision,
)
from app.policies import PolicyEngine
from tests.unit.test_daily_minimum import additional_candidate
from tests.unit.test_employer_relationships import _job, _relationship_graph
from tests.unit.test_policy_and_email import make_graph, settings
from tests.unit.test_prepare_pending_scope import _wire


async def _send_first(session, graph, *, days_ago: float) -> None:
    profile, employer, _first_job, _second_job, first, *_rest = graph
    first.status = ApplicationStatus.SENT
    first.sent_at = datetime.now(UTC) - timedelta(days=days_ago)
    await EmployerRelationshipService().record_event(
        session,
        profile_id=profile.id,
        employer_id=employer.id,
        event_type=EmployerInteractionType.APPLICATION_SENT,
        channel=EmployerInteractionChannel.APPLICATION,
        idempotency_key=f"first-sent:{days_ago}",
        application_id=first.id,
        occurred_at=first.sent_at,
    )


async def _second_outcome(session, graph, *, release_days: int):
    _profile, _employer, _first_job, second_job, _first, second, _first_eval, second_eval = graph
    return await EmployerRelationshipService().policy_outcome(
        session,
        application=second,
        evaluation=second_eval,
        job=second_job,
        unanswered_release_days=release_days,
    )


async def test_employer_stays_frozen_inside_the_unanswered_window(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _relationship_graph(session)
        await _send_first(session, graph, days_ago=13)

        outcome = await _second_outcome(session, graph, release_days=14)

        assert outcome.no_active_conversation is False
        assert outcome.slot_available is False


async def test_employer_is_released_after_the_unanswered_window(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _relationship_graph(session)
        await _send_first(session, graph, days_ago=15)

        outcome = await _second_outcome(session, graph, release_days=14)

        assert outcome.no_active_conversation is True
        assert outcome.slot_available is True
        # The sent application itself stays a recorded fact.
        assert graph[4].status is ApplicationStatus.SENT


async def test_zero_window_keeps_the_previous_permanent_freeze(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _relationship_graph(session)
        await _send_first(session, graph, days_ago=200)

        outcome = await _second_outcome(session, graph, release_days=0)

        assert outcome.slot_available is False


async def test_employer_reply_keeps_the_employer_frozen_after_the_window(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _relationship_graph(session)
        profile, employer = graph[0], graph[1]
        await _send_first(session, graph, days_ago=30)
        await EmployerRelationshipService().record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=EmployerInteractionType.EMPLOYER_REPLIED,
            channel=EmployerInteractionChannel.EMAIL,
            idempotency_key="reply-after-first",
            occurred_at=datetime.now(UTC) - timedelta(days=29),
        )

        outcome = await _second_outcome(session, graph, release_days=14)

        assert outcome.no_active_conversation is False
        assert outcome.slot_available is False


async def test_a_new_send_restarts_the_window(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        graph = await _relationship_graph(session)
        profile, employer, _first_job, second_job, _first, second, first_eval, _second_eval = graph
        await _send_first(session, graph, days_ago=40)
        # The released employer received the second application yesterday.
        second.status = ApplicationStatus.SENT
        second.sent_at = datetime.now(UTC) - timedelta(days=1)
        await EmployerRelationshipService().record_event(
            session,
            profile_id=profile.id,
            employer_id=employer.id,
            event_type=EmployerInteractionType.APPLICATION_SENT,
            channel=EmployerInteractionChannel.APPLICATION,
            idempotency_key="second-sent",
            application_id=second.id,
            occurred_at=second.sent_at,
        )
        source = await session.get(JobSource, second_job.source_id)
        assert source is not None
        third_job = await _job(session, source, key="q", company="Printterra", title="Driver")
        third_job.employer_id = employer.id
        third = Application(
            profile_id=profile.id,
            canonical_job_id=third_job.canonical_job_id,
            employer_id=employer.id,
            source_job_id=third_job.id,
            match_evaluation_id=first_eval.id,
            resume_id=profile.id,
            recipient_contact_id=profile.id,
            subject="fixture",
            body="fixture",
            language="en",
            status=ApplicationStatus.PREPARED,
            idempotency_key="p" * 64,
        )
        session.add(third)
        await session.flush()

        outcome = await EmployerRelationshipService().policy_outcome(
            session,
            application=third,
            evaluation=first_eval,
            job=third_job,
            unanswered_release_days=14,
        )

        assert outcome.slot_available is False


async def _sibling_sent(session, graph, *, days_ago: float) -> None:
    """Another vacancy of the same employer was applied to ``days_ago`` days ago."""

    profile, application = graph[1], graph[8]
    sibling = (await additional_candidate(session, graph, company=graph[5].company, score=90))[4]
    sibling.employer_id = application.employer_id
    sibling.status = ApplicationStatus.SENT
    sibling.sent_at = datetime.now(UTC) - timedelta(days=days_ago)
    await EmployerRelationshipService().record_event(
        session,
        profile_id=profile.id,
        employer_id=application.employer_id,
        event_type=EmployerInteractionType.APPLICATION_SENT,
        channel=EmployerInteractionChannel.APPLICATION,
        idempotency_key=f"sibling-sent:{sibling.id}",
        application_id=sibling.id,
        occurred_at=sibling.sent_at,
    )
    await session.flush()


async def _engine_decision(session, graph, storage: Path, *, release_days: int):
    _source, profile, preference, resume, _canonical, job, evaluation, contact, application = graph
    engine = PolicyEngine(
        settings(storage).model_copy(update={"employer_unanswered_release_days": release_days})
    )
    return await engine.apply(
        session, application, preference, evaluation, job, resume, contact, profile
    )


@pytest.mark.parametrize(
    ("days_ago", "expected"),
    [(5, PolicyDecision.DEFERRED), (15, PolicyDecision.AUTO_APPROVED)],
)
async def test_policy_engine_applies_the_configured_window(
    sqlite_session_factory, tmp_path: Path, days_ago: int, expected: PolicyDecision
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        # The first evaluation resolves the employer identity of the vacancy.
        await _engine_decision(session, graph, tmp_path, release_days=14)
        await _sibling_sent(session, graph, days_ago=days_ago)

        result = await _engine_decision(session, graph, tmp_path, release_days=14)

        assert result.decision is expected


async def test_default_settings_release_an_unanswered_employer_after_14_days(
    tmp_path: Path,
) -> None:
    assert settings(tmp_path).employer_unanswered_release_days == 14


async def _stale_deferred(session_factory, storage: Path):
    """Deferred while the stored policy result still says "auto approved".

    Production had such rows: the status was changed by an employer safety
    pass that did not rewrite the policy result, so no rule was recorded as
    failed and nothing ever re-evaluated them.
    """

    async with session_factory() as session:
        graph = await make_graph(session, storage)
        graph[2].additional_rules = {"minimum_daily_applications": 2}
        application = graph[8]
        result = await PolicyEngine(settings(storage)).apply(
            session, application, graph[2], graph[6], graph[5], graph[3], graph[7], graph[1]
        )
        assert result.decision is PolicyDecision.AUTO_APPROVED
        application.status = ApplicationStatus.DEFERRED
        await session.commit()
        return graph[1].id, application.id


async def test_scheduler_reevaluates_a_deferral_no_failed_rule_explains(
    sqlite_session_factory, tmp_path: Path
) -> None:
    _profile_id, application_id = await _stale_deferred(sqlite_session_factory, tmp_path)

    with pytest.MonkeyPatch.context() as patch:
        _wire(patch, sqlite_session_factory, tmp_path)
        await application_service.prepare_pending_applications()

    async with sqlite_session_factory() as session:
        application = await session.get(Application, application_id)
        assert application is not None
        # Nothing actually blocks it, so the policy approves it again instead
        # of leaving it deferred forever.
        assert application.status is ApplicationStatus.AUTO_APPROVED


@pytest.mark.parametrize("status", [ApplicationStatus.DEFERRED, ApplicationStatus.BLOCKED])
async def test_minimum_audit_does_not_call_a_deferred_or_blocked_application_ready(
    sqlite_session_factory, tmp_path: Path, status: ApplicationStatus
) -> None:
    profile_id, application_id = await _stale_deferred(sqlite_session_factory, tmp_path)
    async with sqlite_session_factory() as session:
        application = await session.get(Application, application_id)
        assert application is not None
        application.status = status
        await session.flush()

        audit = await daily_minimum_audit(session, profile_id)

        assert audit["primary_blockers"]["ready"] == 0
        assert all(item["unique_ready_employers"] == 0 for item in audit["replay"])
        expected = "employer" if status is ApplicationStatus.DEFERRED else "hard_safety"
        assert audit["primary_blockers"][expected] == 1
