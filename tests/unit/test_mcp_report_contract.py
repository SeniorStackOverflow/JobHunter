from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.email.providers import FakeGmailProvider
from app.email.service import EmailService
from app.models.entities import (
    Application,
    AuditEvent,
    DailyReport,
    EmailDelivery,
    EmailSendAttempt,
    MatchEvaluation,
)
from app.models.enums import ApplicationStatus, DeliveryStatus, MatchDecision, PolicyDecision
from app.reports.service import _generate
from tests.unit.test_daily_minimum import additional_candidate
from tests.unit.test_policy_and_email import make_graph, settings


async def _row_counts(session_factory) -> dict[str, int]:
    async with session_factory() as session:
        return {
            model.__name__: int(await session.scalar(select(func.count(model.id))) or 0)
            for model in (DailyReport, AuditEvent, EmailSendAttempt, EmailDelivery, MatchEvaluation)
        }


async def test_mcp_daily_report_explains_bounce_and_bound_score_read_only(
    sqlite_session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.database import session as database_session
    from app.mcp import server as mcp_server

    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        graph[2].maximum_daily_applications = 5
        second = await additional_candidate(
            session, graph, company="Second Company", score=92, decision=MatchDecision.AUTO_APPLY
        )
        applications = [graph[8], second[4]]
        for application in applications:
            application.status = ApplicationStatus.AUTO_APPROVED
            application.policy_decision = PolicyDecision.AUTO_APPROVED
        accepted_id, bounced_id = (application.id for application in applications)
        await session.commit()

    service = EmailService(settings(tmp_path), sqlite_session_factory, FakeGmailProvider())
    for identifier in (accepted_id, bounced_id):
        delivery = await service.send_application(identifier)
        assert delivery.status is DeliveryStatus.PROVIDER_ACCEPTED

    async with sqlite_session_factory() as session:
        bounced = await session.scalar(
            select(EmailDelivery).where(EmailDelivery.application_id == bounced_id)
        )
        bounced_application = await session.get(Application, bounced_id)
        accepted_application = await session.get(Application, accepted_id)
        assert bounced is not None and bounced_application is not None
        assert accepted_application is not None
        bounced.status = DeliveryStatus.DOMAIN_REJECTED
        bounced.bounced_at = datetime.now(UTC)
        bounced_application.status = ApplicationStatus.FAILED
        bound = await session.get(MatchEvaluation, accepted_application.match_evaluation_id)
        assert bound is not None
        values = {
            column.key: getattr(bound, column.key)
            for column in MatchEvaluation.__table__.columns
            if column.key != "id"
        }
        values.update(
            overall_fit=0,
            decision=MatchDecision.PREPARE_FOR_REVIEW,
            risks=["llm_provider_failure:llmrouter:schema_validation:literal_error"],
            created_at=bound.created_at + timedelta(minutes=5),
        )
        session.add(MatchEvaluation(**values))
        await session.commit()
        bound_id = str(bound.id)

    monkeypatch.setattr(database_session, "async_session_factory", sqlite_session_factory)
    before = await _row_counts(sqlite_session_factory)
    report = await mcp_server.get_daily_report()
    assert await _row_counts(sqlite_session_factory) == before

    summary = report["summary"]
    delivery = summary["email_delivery"]
    assert delivery["provider_submissions"] == 2
    assert delivery["initially_accepted"] == 2
    assert delivery["submitted_cohort_current_status"] == {
        "provider_accepted": 1,
        "domain_rejected": 1,
    }
    [sent] = summary["sent_applications"]
    assert sent["application_id"] == str(accepted_id)
    assert sent["overall_score"] == 92
    assert sent["match_evaluation_id"] == bound_id
    assert sent["latest_evaluation_score"] == 0
    assert summary["daily_limit_used"] == 2
    assert summary["report_source"] == "live"
    assert summary["scopes"]["external_calls"] == "system"
    assert summary["scopes"]["matching"] == "default_profile"
    assert "email_delivery.provider_submissions" in summary["counter_definitions"]
    assert summary["collection_started_at"] <= summary["snapshot_at"]

    async with sqlite_session_factory() as session:
        direct = await _generate(session, persist=False)
    assert direct.summary["email_delivery"] == delivery
    assert direct.summary["sent_applications"] == summary["sent_applications"]
