from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.applications.daily_target import daily_target_state
from app.applications.diagnostics import daily_minimum_audit
from app.applications.service import ApplicationService, generate_letter
from app.email.providers import FakeGmailProvider
from app.email.service import EmailService
from app.matching.bindings import confirmed_fact_hashes, preference_fingerprint, profile_fingerprint
from app.matching.source_version import compute_source_matching_hash
from app.models.entities import (
    Application,
    CanonicalJob,
    EmployerContact,
    MatchEvaluation,
    SourceJob,
)
from app.models.enums import ApplicationStatus, MatchDecision, PolicyDecision
from app.policies import PolicyEngine
from app.reports.service import _generate
from app.time_utils import local_day_bounds
from tests.unit.test_policy_and_email import make_graph, settings


async def additional_candidate(
    session,
    graph,
    *,
    company: str,
    score: int,
    decision=MatchDecision.PREPARE_FOR_REVIEW,
    soft=False,
):
    _, profile, preference, _resume, canonical, job, evaluation, contact, application = graph
    suffix = uuid4().hex

    def copy(model, original, **changes):
        values = {
            column.key: getattr(original, column.key)
            for column in model.__table__.columns
            if column.key != "id"
        }
        values.update(changes)
        return model(**values)

    next_canonical = copy(
        CanonicalJob,
        canonical,
        employer_id=None,
        canonical_fingerprint=suffix * 2,
        primary_source_job_id=None,
        normalized_company=company.casefold(),
    )
    session.add(next_canonical)
    await session.flush()
    next_job = copy(
        SourceJob,
        job,
        employer_id=None,
        canonical_job_id=next_canonical.id,
        company=company,
        external_job_id=suffix,
        source_fingerprint=suffix * 2,
        canonical_url=f"https://jobs.example.com/{suffix}",
        public_email=f"jobs@{suffix}.example.test",
        public_emails=[],
        raw_metadata={},
    )
    next_job.matching_content_hash = compute_source_matching_hash(next_job)
    session.add(next_job)
    await session.flush()
    next_canonical.primary_source_job_id = next_job.id
    next_evaluation = copy(
        MatchEvaluation,
        evaluation,
        canonical_job_id=next_canonical.id,
        source_job_id=next_job.id,
        source_matching_hash=next_job.matching_content_hash,
        decision=decision,
        overall_fit=score,
        soft_mismatches=["skills"] if soft else [],
        preference_fingerprint=preference_fingerprint(preference),
        profile_fingerprint=profile_fingerprint(profile),
        confirmed_fact_hashes=confirmed_fact_hashes(profile),
    )
    next_contact = copy(
        EmployerContact,
        contact,
        employer_id=None,
        canonical_job_id=next_canonical.id,
        source_job_id=next_job.id,
        value=next_job.public_email,
        evidence_url=next_job.canonical_url,
        official_domain=f"{suffix}.example.test",
    )
    session.add_all([next_evaluation, next_contact])
    await session.flush()
    subject, body, language, facts = generate_letter(profile, next_job)
    next_application = copy(
        Application,
        application,
        employer_id=None,
        canonical_job_id=next_canonical.id,
        source_job_id=next_job.id,
        match_evaluation_id=next_evaluation.id,
        recipient_contact_id=next_contact.id,
        status=ApplicationStatus.PREPARED,
        policy_decision=None,
        policy_result={},
        subject=subject,
        body=body,
        language=language,
        used_confirmed_facts=facts,
        idempotency_key=suffix * 2,
        sent_at=None,
    )
    session.add(next_application)
    await session.flush()
    return next_canonical, next_job, next_evaluation, next_contact, next_application


async def apply_candidate(session, engine, graph, candidate):
    _, profile, preference, resume, *_ = graph
    _, job, evaluation, contact, application = candidate
    return await engine.apply(
        session, application, preference, evaluation, job, resume, contact, profile
    )


@pytest.mark.e2e
async def test_aggressive_minimum_sends_distinct_companies_then_restores_normal(
    sqlite_session_factory,
    tmp_path: Path,
) -> None:
    provider = FakeGmailProvider()
    config = settings(tmp_path)
    engine = PolicyEngine(config)
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        preference = graph[2]
        preference.additional_rules = {"minimum_daily_applications": 2}
        preference.maximum_daily_applications = 20
        preference.minimum_auto_send_score = 70
        first = (graph[4], graph[5], graph[6], graph[7], graph[8])
        weaker = await additional_candidate(
            session,
            graph,
            company="Second Company",
            score=55,
            decision=MatchDecision.SKIP,
            soft=True,
        )
        excess = await additional_candidate(session, graph, company="Third Company", score=45)
        normal = await additional_candidate(
            session, graph, company="Fourth Company", score=95, decision=MatchDecision.AUTO_APPLY
        )
        same_company = await additional_candidate(
            session, graph, company="Example Company", score=96, decision=MatchDecision.AUTO_APPLY
        )
        assert (
            await apply_candidate(session, engine, graph, first)
        ).decision is PolicyDecision.AUTO_APPROVED
        result = await apply_candidate(session, engine, graph, weaker)
        assert result.decision is PolicyDecision.AUTO_APPROVED
        assert result.catchup_stage == 50
        assert (await daily_target_state(session, preference)).remaining == 0
        assert (
            await apply_candidate(session, engine, graph, excess)
        ).decision is not PolicyDecision.AUTO_APPROVED
        first_id, weaker_id, excess_id = first[-1].id, weaker[-1].id, excess[-1].id
        normal_id, duplicate_id = normal[-1].id, same_company[-1].id
        await session.commit()
    sender = EmailService(config, sqlite_session_factory, provider)
    await sender.send_application(first_id)
    await sender.send_application(weaker_id)
    async with sqlite_session_factory() as session:
        preference = await session.get(type(preference), preference.id)
        target = await daily_target_state(session, preference)
        assert target.sent == 2 and target.remaining == 0 and target.reserved == 0
        assert len(target.sent_employers) == 2
        service = ApplicationService(config)
        await service.reevaluate_policy(session, await session.get(Application, excess_id))
        assert (
            await session.get(Application, excess_id)
        ).status is not ApplicationStatus.AUTO_APPROVED
        await service.reevaluate_policy(session, await session.get(Application, normal_id))
        assert (await session.get(Application, normal_id)).status is ApplicationStatus.AUTO_APPROVED
        await service.reevaluate_policy(session, await session.get(Application, duplicate_id))
        assert (await session.get(Application, duplicate_id)).status is ApplicationStatus.DEFERRED
        await session.commit()
    await sender.send_application(normal_id)
    assert len(provider.outbox) == 3
    assert len({message.recipient for message in provider.outbox}) == 3
    assert provider.outbox[1].application_id == str(weaker_id)


@pytest.mark.parametrize("obstacle", ["mandatory", "risk", "scam", "unclassified_skip"])
async def test_aggressive_minimum_does_not_override_non_soft_failures(
    sqlite_session_factory,
    tmp_path: Path,
    obstacle: str,
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        graph[2].additional_rules = {"minimum_daily_applications": 2}
        graph[6].overall_fit = 45
        graph[6].decision = MatchDecision.PREPARE_FOR_REVIEW
        if obstacle == "mandatory":
            graph[6].missing_requirements = ["mandatory work permit"]
        elif obstacle == "risk":
            graph[6].risks = ["unconfirmed shift availability"]
        elif obstacle == "scam":
            graph[6].scam_indicators = ["upfront payment"]
        else:
            graph[6].decision = MatchDecision.SKIP
        result = await apply_candidate(
            session,
            PolicyEngine(settings(tmp_path)),
            graph,
            (graph[4], graph[5], graph[6], graph[7], graph[8]),
        )
        assert result.decision is not PolicyDecision.AUTO_APPROVED
        assert result.catchup_stage is None


async def test_reservation_released_on_cancel_pause_source_failure_and_day_rollover(
    sqlite_session_factory,
    tmp_path: Path,
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        source, _, preference, _, _, _, _, _, application = graph
        preference.additional_rules = {"minimum_daily_applications": 1}
        candidate = (graph[4], graph[5], graph[6], graph[7], application)
        await apply_candidate(session, PolicyEngine(settings(tmp_path)), graph, candidate)
        assert (await daily_target_state(session, preference)).reserved == 1
        assert (
            await daily_target_state(session, preference, now=datetime.now(UTC) + timedelta(days=1))
        ).reserved == 0
        preference.global_pause = True
        assert (await daily_target_state(session, preference)).remaining == 1
        preference.global_pause = False
        source.automatic_actions_paused = True
        assert (await daily_target_state(session, preference)).reserved == 0
        source.automatic_actions_paused = False
        await ApplicationService(settings(tmp_path)).reject(session, application.id)
        assert (await daily_target_state(session, preference)).reserved == 0
        returned = await ApplicationService(settings(tmp_path)).prepare(
            session, application.canonical_job_id, application.profile_id
        )
        assert returned.status is ApplicationStatus.CANCELLED
        assert returned.policy_result["owner_rejected"] is True


async def test_finished_report_is_previous_day_and_immutable(sqlite_session_factory, tmp_path):
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        local_start, _, _ = local_day_bounds()
        yesterday = local_start.date() - timedelta(days=1)
        application = graph[-1]
        application.status = ApplicationStatus.SENT
        application.sent_at = local_day_bounds(day=yesterday)[1] + timedelta(hours=10)
        await session.flush()
        report = await _generate(session, day=yesterday)
        assert report.summary["finalized"] is True
        assert report.summary["daily_sent"] == 1
        application.sent_at = datetime.now(UTC)
        await session.flush()
        assert (await _generate(session, day=yesterday)).summary["daily_sent"] == 1


async def test_minimum_audit_has_exclusive_funnel_and_does_not_mutate(
    sqlite_session_factory, tmp_path
):
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        graph[2].additional_rules = {"minimum_daily_applications": 2}
        await apply_candidate(
            session,
            PolicyEngine(settings(tmp_path)),
            graph,
            (graph[4], graph[5], graph[6], graph[7], graph[8]),
        )
        assert not session.dirty and not session.new
        report = await daily_minimum_audit(session, graph[1].id)
        assert sum(report["primary_blockers"].values()) == report["unique_jobs"] == 1
        assert report["reserved"] == 1 and report["remaining"] == 1
        assert all(item["unique_ready_employers"] == 0 for item in report["replay"])
        assert not session.dirty and not session.new


@pytest.mark.parametrize("score, expected", [(65, 60), (55, 50), (45, 40)])
async def test_catchup_stages_use_least_required_relaxation(
    sqlite_session_factory, tmp_path, score, expected
):
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        graph[2].additional_rules = {"minimum_daily_applications": 1}
        graph[2].minimum_auto_send_score = 70
        graph[6].decision = MatchDecision.PREPARE_FOR_REVIEW
        graph[6].overall_fit = score
        result = await apply_candidate(
            session,
            PolicyEngine(settings(tmp_path)),
            graph,
            (graph[4], graph[5], graph[6], graph[7], graph[8]),
        )
        assert result.decision is PolicyDecision.AUTO_APPROVED
        assert result.catchup_stage == expected


@pytest.mark.parametrize("optional_count", [0, 1, 2])
async def test_aggressive_catchup_accepts_at_most_one_explicit_optional_gap(
    sqlite_session_factory, tmp_path, optional_count
):
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        graph[2].additional_rules = {"minimum_daily_applications": 1}
        graph[2].minimum_auto_send_score = 70
        graph[6].decision = MatchDecision.PREPARE_FOR_REVIEW
        graph[6].overall_fit = 45
        graph[6].optional_requirements_missing = [
            f"Optional advantage {index}" for index in range(optional_count)
        ]
        result = await apply_candidate(
            session,
            PolicyEngine(settings(tmp_path)),
            graph,
            (graph[4], graph[5], graph[6], graph[7], graph[8]),
        )
        assert (result.decision is PolicyDecision.AUTO_APPROVED) is (optional_count <= 1)


async def test_failed_delivery_reopens_deficit_for_a_different_company(
    sqlite_session_factory, tmp_path
):
    config = settings(tmp_path)
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        preference = graph[2]
        preference.additional_rules = {"minimum_daily_applications": 1}
        candidate = (graph[4], graph[5], graph[6], graph[7], graph[8])
        await apply_candidate(session, PolicyEngine(config), graph, candidate)
        next_candidate = await additional_candidate(session, graph, company="Replacement", score=45)
        application_id, profile_id = graph[-1].id, graph[1].id
        await session.commit()
    await EmailService(
        config, sqlite_session_factory, FakeGmailProvider("permanent")
    ).send_application(application_id)
    async with sqlite_session_factory() as session:
        preference = await session.get(type(preference), preference.id)
        assert (await daily_target_state(session, preference)).remaining == 1
        application = await session.get(Application, next_candidate[-1].id)
        await ApplicationService(config).reevaluate_policy(session, application)
        assert application.status is ApplicationStatus.AUTO_APPROVED
        assert application.profile_id == profile_id
        assert application.policy_result["catchup_stage"] == 40


async def test_preparation_selects_best_company_before_writing_other_letters(
    sqlite_session_factory,
    tmp_path,
    monkeypatch,
):
    import app.applications.service as applications
    import app.database.session as database

    monkeypatch.setattr(database, "async_session_factory", sqlite_session_factory)
    monkeypatch.setattr(applications, "get_settings", lambda: settings(tmp_path))
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        graph[2].additional_rules = {"minimum_daily_applications": 2}
        graph[2].minimum_auto_send_score = 70
        graph[6].overall_fit = 45
        graph[6].decision = MatchDecision.PREPARE_FOR_REVIEW
        better = await additional_candidate(session, graph, company="Example Company", score=55)
        other = await additional_candidate(session, graph, company="Another Company", score=45)
        ids = (graph[-1].id, better[-1].id, other[-1].id)
        await session.commit()
    await applications.prepare_pending_applications()
    async with sqlite_session_factory() as session:
        first, best, other = [await session.get(Application, identifier) for identifier in ids]
        assert best.status is ApplicationStatus.AUTO_APPROVED
        assert other.status is ApplicationStatus.AUTO_APPROVED
        assert first.status is ApplicationStatus.PREPARED
        assert (
            await daily_target_state(session, await session.get(type(graph[2]), graph[2].id))
        ).reserved == 2


async def test_source_demand_stops_when_reserve_is_ready_and_honors_owner_selection(
    sqlite_session_factory,
    tmp_path,
    monkeypatch,
):
    from app.profiles.sources import set_source_selected
    from app.scheduler import tasks

    monkeypatch.setattr(tasks, "async_session_factory", sqlite_session_factory)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings(tmp_path))
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        graph[2].additional_rules = {"minimum_daily_applications": 1}
        source_id, profile_id = graph[0].id, graph[1].id
        await session.commit()
    assert await tasks._minimum_search_sources() == {source_id}
    async with sqlite_session_factory() as session:
        await set_source_selected(
            session, profile_id=profile_id, source_id=source_id, enabled=False
        )
        await session.commit()
    assert await tasks._minimum_search_sources() == set()
    async with sqlite_session_factory() as session:
        await set_source_selected(session, profile_id=profile_id, source_id=source_id, enabled=True)
        application = await session.get(Application, graph[-1].id)
        await ApplicationService(settings(tmp_path)).reevaluate_policy(session, application)
        await session.commit()
    assert await tasks._minimum_search_sources() == set()


@pytest.mark.parametrize(
    "obstacle", [None, "late_discovered_reply", "fresh", "reply", "unknown", "mailbox"]
)
async def test_unanswered_closure_is_explicit_and_keeps_frequency_cooldown(
    sqlite_session_factory,
    tmp_path,
    obstacle,
):
    from app.employers import EmployerRelationshipService
    from app.models.entities import EmailMailboxCursor
    from app.models.enums import (
        EmployerInteractionChannel,
        EmployerInteractionType,
        SuppressionScope,
    )

    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        application = graph[-1]
        await apply_candidate(
            session,
            PolicyEngine(settings(tmp_path)),
            graph,
            (graph[4], graph[5], graph[6], graph[7], application),
        )
        application.status = ApplicationStatus.SENT
        sent_at = datetime.now(UTC) - timedelta(days=10 if obstacle == "fresh" else 46)
        application.sent_at = sent_at
        service = EmployerRelationshipService()
        await service.record_event(
            session,
            profile_id=graph[1].id,
            employer_id=application.employer_id,
            application_id=application.id,
            event_type=EmployerInteractionType.APPLICATION_SENT,
            channel=EmployerInteractionChannel.EMAIL,
            idempotency_key="old-unanswered",
            occurred_at=sent_at,
        )
        if obstacle != "mailbox":
            session.add(
                EmailMailboxCursor(
                    account_id=graph[1].owner_account_id,
                    provider="gmail",
                    last_checked_at=datetime.now(UTC),
                )
            )
        if obstacle == "reply":
            await service.record_event(
                session,
                profile_id=graph[1].id,
                employer_id=application.employer_id,
                event_type=EmployerInteractionType.EMPLOYER_REPLIED,
                channel=EmployerInteractionChannel.EMAIL,
                idempotency_key="reply",
            )
        if obstacle == "unknown":
            application.status = ApplicationStatus.DELIVERY_UNKNOWN
        await session.flush()
        if obstacle not in {None, "late_discovered_reply"}:
            with pytest.raises(ValueError):
                await service.close_unanswered(
                    session,
                    profile_id=graph[1].id,
                    employer_id=application.employer_id,
                    actor="owner",
                    reason="No answer",
                )
        else:
            relationship = await service.close_unanswered(
                session,
                profile_id=graph[1].id,
                employer_id=application.employer_id,
                actor="owner",
                reason="No answer",
            )
            assert relationship.suppression_scope is SuppressionScope.EMPLOYER
            assert relationship.suppressed_until == sent_at + timedelta(days=90)
            assert application.status is ApplicationStatus.SENT
            await service.record_event(
                session,
                profile_id=graph[1].id,
                employer_id=application.employer_id,
                application_id=application.id,
                event_type=EmployerInteractionType.EMPLOYER_REPLIED,
                channel=EmployerInteractionChannel.EMAIL,
                idempotency_key="reply-after-close",
                occurred_at=sent_at + timedelta(days=1)
                if obstacle == "late_discovered_reply"
                else datetime.now(UTC),
            )
            from app.models.enums import EmployerRelationshipState

            assert relationship.state is EmployerRelationshipState.EMPLOYER_REPLIED


async def test_replay_can_include_soft_skips_but_never_owner_rejections(
    sqlite_session_factory, tmp_path
):
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        graph[2].additional_rules = {"minimum_daily_applications": 2}
        await apply_candidate(
            session,
            PolicyEngine(settings(tmp_path)),
            graph,
            (graph[4], graph[5], graph[6], graph[7], graph[8]),
        )
        evaluation, application = graph[6], graph[8]
        evaluation.decision = MatchDecision.SKIP
        evaluation.overall_fit = 45
        evaluation.soft_mismatches = ["skills"]
        application.status = ApplicationStatus.CANCELLED
        application.policy_decision = PolicyDecision.SKIPPED
        application.policy_result = {
            "rules_failed": ["match_not_skipped", "overall_score_threshold"]
        }
        await session.flush()
        audit = await daily_minimum_audit(session, graph[1].id)
        assert audit["replay"][-1]["unique_ready_employers"] == 1
        assert audit["replay"][0]["unique_ready_employers"] == 0
        application.policy_result = {**application.policy_result, "owner_rejected": True}
        await session.flush()
        audit = await daily_minimum_audit(session, graph[1].id)
        assert all(item["unique_ready_employers"] == 0 for item in audit["replay"])
        assert audit["primary_blockers"]["hard_safety"] == 1
