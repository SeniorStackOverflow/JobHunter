from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.applications import service as application_service
from app.models.entities import Account, Application, MatchEvaluation, UserProfile
from app.models.enums import (
    AccountStatus,
    ApplicationStatus,
    MatchDecision,
    PolicyDecision,
    ProfileStatus,
)
from tests.unit.test_daily_minimum import additional_candidate
from tests.unit.test_policy_and_email import make_graph, settings


async def _evaluated_pair_without_application(session_factory, storage: Path) -> UUID:
    """One evaluated (profile, canonical job) pair that has no Application yet."""

    async with session_factory() as session:
        graph = await make_graph(session, storage)
        profile_id = graph[1].id
        await session.execute(delete(Application).where(Application.profile_id == profile_id))
        await session.commit()
        return profile_id


def _count_prepare_calls(monkeypatch: pytest.MonkeyPatch) -> list[UUID | None]:
    calls: list[UUID | None] = []
    original = application_service.ApplicationService.prepare

    async def counting_prepare(self, session, canonical_job_id, profile_id=None, **kwargs):
        calls.append(profile_id)
        return await original(self, session, canonical_job_id, profile_id, **kwargs)

    monkeypatch.setattr(application_service.ApplicationService, "prepare", counting_prepare)
    return calls


def _wire(monkeypatch: pytest.MonkeyPatch, session_factory, storage: Path) -> None:
    current_settings = settings(storage)
    monkeypatch.setattr("app.database.session.async_session_factory", session_factory)
    monkeypatch.setattr(application_service, "get_settings", lambda: current_settings)


async def test_active_profile_pair_is_still_prepared(
    sqlite_session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_id = await _evaluated_pair_without_application(sqlite_session_factory, tmp_path)
    _wire(monkeypatch, sqlite_session_factory, tmp_path)
    calls = _count_prepare_calls(monkeypatch)

    assert await application_service.prepare_pending_applications() == 1
    assert calls == [profile_id]


async def test_draft_profile_pairs_are_not_attempted_every_cycle(
    sqlite_session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_id = await _evaluated_pair_without_application(sqlite_session_factory, tmp_path)
    async with sqlite_session_factory() as session:
        profile = await session.get(UserProfile, profile_id)
        assert profile is not None
        profile.status = ProfileStatus.DRAFT
        await session.commit()
    _wire(monkeypatch, sqlite_session_factory, tmp_path)
    calls = _count_prepare_calls(monkeypatch)

    assert await application_service.prepare_pending_applications() == 0
    # The evaluations of a profile that may not be processed must not cost a
    # prepare() round trip per pair on every scheduler cycle.
    assert calls == []
    async with sqlite_session_factory() as session:
        assert (await session.scalars(select(Application))).all() == []


async def test_suspended_account_pairs_are_not_attempted(
    sqlite_session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_id = await _evaluated_pair_without_application(sqlite_session_factory, tmp_path)
    async with sqlite_session_factory() as session:
        account = Account(status=AccountStatus.SUSPENDED)
        session.add(account)
        await session.flush()
        profile = await session.get(UserProfile, profile_id)
        assert profile is not None
        profile.owner_account_id = account.id
        await session.commit()
    _wire(monkeypatch, sqlite_session_factory, tmp_path)
    calls = _count_prepare_calls(monkeypatch)

    assert await application_service.prepare_pending_applications() == 0
    assert calls == []


async def test_only_processing_profile_is_attempted_when_profiles_are_mixed(
    sqlite_session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active_id = await _evaluated_pair_without_application(sqlite_session_factory, tmp_path)
    async with sqlite_session_factory() as session:
        evaluation = await session.scalar(
            select(MatchEvaluation).where(MatchEvaluation.profile_id == active_id)
        )
        assert evaluation is not None
        draft = UserProfile(id=uuid4(), name="Draft owner", status=ProfileStatus.DRAFT)
        session.add(draft)
        await session.flush()
        values = {
            column.key: getattr(evaluation, column.key)
            for column in MatchEvaluation.__table__.columns
            if column.key != "id"
        }
        session.add(MatchEvaluation(**{**values, "profile_id": draft.id, "resume_id": None}))
        await session.commit()
        draft_id = draft.id
    _wire(monkeypatch, sqlite_session_factory, tmp_path)
    calls = _count_prepare_calls(monkeypatch)

    assert await application_service.prepare_pending_applications() == 1

    assert draft_id not in calls
    assert calls == [active_id]


async def _run_cycle(monkeypatch: pytest.MonkeyPatch) -> list[UUID | None]:
    calls = _count_prepare_calls(monkeypatch)
    await application_service.prepare_pending_applications()
    monkeypatch.undo()
    return calls


async def _only_application(session_factory, profile_id: UUID) -> Application | None:
    async with session_factory() as session:
        return await session.scalar(select(Application).where(Application.profile_id == profile_id))


@pytest.mark.parametrize(
    ("decision", "fit", "risks", "expected_status", "expected_policy"),
    [
        (MatchDecision.SKIP, 30, [], ApplicationStatus.CANCELLED, PolicyDecision.SKIPPED),
        (
            MatchDecision.PREPARE_FOR_REVIEW,
            60,
            ["salary_unclear"],
            ApplicationStatus.PENDING_REVIEW,
            PolicyDecision.PENDING_REVIEW,
        ),
    ],
    ids=["skip", "review"],
)
async def test_new_pair_gets_a_durable_application_and_is_not_retried(
    sqlite_session_factory,
    tmp_path: Path,
    decision: MatchDecision,
    fit: int,
    risks: list[str],
    expected_status: ApplicationStatus,
    expected_policy: PolicyDecision,
) -> None:
    profile_id = await _evaluated_pair_without_application(sqlite_session_factory, tmp_path)
    async with sqlite_session_factory() as session:
        evaluation = await session.scalar(
            select(MatchEvaluation).where(MatchEvaluation.profile_id == profile_id)
        )
        assert evaluation is not None
        evaluation.decision, evaluation.overall_fit, evaluation.risks = decision, fit, risks
        await session.commit()

    with pytest.MonkeyPatch.context() as patch:
        _wire(patch, sqlite_session_factory, tmp_path)
        first = await _run_cycle(patch)
    assert first == [profile_id]
    application = await _only_application(sqlite_session_factory, profile_id)
    # A vacancy that cannot be auto-sent still gets its policy outcome recorded:
    # review items reach the owner's queue and skipped ones are terminal.
    assert application is not None
    assert application.status is expected_status
    assert application.policy_decision is expected_policy

    with pytest.MonkeyPatch.context() as patch:
        _wire(patch, sqlite_session_factory, tmp_path)
        second = await _run_cycle(patch)
    assert second == []


async def test_existing_application_is_rebound_to_a_newer_non_auto_evaluation(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        profile_id, evaluation, application = graph[1].id, graph[6], graph[8]
        application.status = ApplicationStatus.PENDING_REVIEW
        application.policy_decision = PolicyDecision.PENDING_REVIEW
        application.policy_result = {"rules_failed": ["no_material_match_risk"]}
        values = {
            column.key: getattr(evaluation, column.key)
            for column in MatchEvaluation.__table__.columns
            if column.key != "id"
        }
        newer = MatchEvaluation(
            **{
                **values,
                "decision": MatchDecision.PREPARE_FOR_REVIEW,
                "overall_fit": 60,
                "risks": ["salary_unclear"],
                "created_at": evaluation.created_at + timedelta(minutes=5),
            }
        )
        session.add(newer)
        await session.commit()
        newer_id, application_id = newer.id, application.id

    with pytest.MonkeyPatch.context() as patch:
        _wire(patch, sqlite_session_factory, tmp_path)
        first = await _run_cycle(patch)
    assert first == [profile_id]
    async with sqlite_session_factory() as session:
        refreshed = await session.get(Application, application_id)
        assert refreshed is not None
        # The owner's review item must show the newest evaluation, not stay
        # bound to a superseded one and be retried on every cycle.
        assert refreshed.match_evaluation_id == newer_id
        assert refreshed.status is ApplicationStatus.PENDING_REVIEW

    with pytest.MonkeyPatch.context() as patch:
        _wire(patch, sqlite_session_factory, tmp_path)
        second = await _run_cycle(patch)
    assert second == []


async def test_same_company_pair_is_deferred_once_instead_of_retried(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        profile_id = graph[1].id
        sibling = await additional_candidate(
            session, graph, company="Example Company", score=85, decision=MatchDecision.AUTO_APPLY
        )
        sibling_canonical_id = sibling[0].id
        await session.execute(delete(Application).where(Application.id == sibling[4].id))
        await session.commit()

    cycles: list[list[UUID | None]] = []
    for _ in range(3):
        with pytest.MonkeyPatch.context() as patch:
            _wire(patch, sqlite_session_factory, tmp_path)
            cycles.append(await _run_cycle(patch))

    async with sqlite_session_factory() as session:
        applications = {
            item.canonical_job_id: item
            for item in (
                await session.scalars(
                    select(Application).where(Application.profile_id == profile_id)
                )
            ).all()
        }
    approved = [
        item for item in applications.values() if item.status is ApplicationStatus.AUTO_APPROVED
    ]
    # Exactly one application per company is reserved; the other one is recorded
    # as deferred instead of being re-attempted without a trace.
    assert len(approved) == 1
    assert applications[sibling_canonical_id].status is ApplicationStatus.DEFERRED
    assert cycles[2] == []
