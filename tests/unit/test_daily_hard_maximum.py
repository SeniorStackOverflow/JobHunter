from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.applications.reconciliation import reconcile_stale_delivery_unknown
from app.delivery_ledger import (
    OUTCOME_DELIVERY_UNKNOWN,
    OUTCOME_IN_FLIGHT,
    OUTCOME_NOT_TRANSMITTED,
    OUTCOME_PROVIDER_ACCEPTED,
    local_day_key,
    transmissions_on_day,
)
from app.email.providers import FakeGmailProvider
from app.email.service import EmailService
from app.models.entities import Application, EmailDelivery, EmailSendAttempt
from app.models.enums import ApplicationStatus, DeliveryStatus, PolicyDecision
from app.policies import PolicyEngine
from tests.unit.test_policy_and_email import make_graph, settings


async def _approved_graph(session_factory, storage: Path, *, maximum: int = 1):
    async with session_factory() as session:
        values = await make_graph(session, storage)
        preference, application = values[2], values[8]
        preference.maximum_daily_applications = maximum
        application.status = ApplicationStatus.AUTO_APPROVED
        application.policy_decision = PolicyDecision.AUTO_APPROVED
        await session.commit()
        return application.id, application.profile_id


async def _daily_limit_failed(session_factory, storage: Path, application_id) -> bool:
    async with session_factory() as session:
        application = await session.get(Application, application_id)
        assert application is not None
        from app.models.entities import (
            EmployerContact,
            JobPreference,
            MatchEvaluation,
            Resume,
            SourceJob,
            UserProfile,
        )

        preference = await session.scalar(
            select(JobPreference).where(JobPreference.profile_id == application.profile_id)
        )
        result = await PolicyEngine(settings(storage)).evaluate(
            session,
            application,
            preference,
            await session.get(MatchEvaluation, application.match_evaluation_id),
            await session.get(SourceJob, application.source_job_id),
            await session.get(Resume, application.resume_id),
            await session.get(EmployerContact, application.recipient_contact_id),
            await session.get(UserProfile, application.profile_id),
        )
        return "daily_limit" in result.rules_failed


async def _attempts(session_factory, application_id) -> list[EmailSendAttempt]:
    async with session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(EmailSendAttempt)
                    .where(EmailSendAttempt.application_id == application_id)
                    .order_by(EmailSendAttempt.attempt_no)
                )
            ).all()
        )


async def test_accepted_then_permanent_bounce_keeps_hard_maximum_consumed(
    sqlite_session_factory, tmp_path: Path
) -> None:
    application_id, profile_id = await _approved_graph(sqlite_session_factory, tmp_path)
    assert not await _daily_limit_failed(sqlite_session_factory, tmp_path, application_id)

    service = EmailService(settings(tmp_path), sqlite_session_factory, FakeGmailProvider())
    delivery = await service.send_application(application_id)
    assert delivery.status is DeliveryStatus.PROVIDER_ACCEPTED

    # A later DSN reports that the recipient domain rejected the accepted message.
    async with sqlite_session_factory() as session:
        stored = await session.scalar(
            select(EmailDelivery).where(EmailDelivery.application_id == application_id)
        )
        application = await session.get(Application, application_id)
        assert stored is not None and application is not None
        stored.status = DeliveryStatus.DOMAIN_REJECTED
        stored.bounced_at = datetime.now(UTC)
        application.status = ApplicationStatus.FAILED
        await session.commit()
        assert await transmissions_on_day(session, profile_id=profile_id) == 1

    assert await _daily_limit_failed(sqlite_session_factory, tmp_path, application_id)
    attempts = await _attempts(sqlite_session_factory, application_id)
    assert [item.outcome for item in attempts] == [OUTCOME_PROVIDER_ACCEPTED]
    assert attempts[0].local_day == local_day_key()


@pytest.mark.parametrize("failure_mode", ["temporary", "permanent"])
async def test_provider_refusal_before_acceptance_releases_reservation(
    sqlite_session_factory, tmp_path: Path, failure_mode: str
) -> None:
    application_id, profile_id = await _approved_graph(sqlite_session_factory, tmp_path)
    service = EmailService(
        settings(tmp_path), sqlite_session_factory, FakeGmailProvider(failure_mode=failure_mode)
    )
    await service.send_application(application_id)

    attempts = await _attempts(sqlite_session_factory, application_id)
    assert [item.outcome for item in attempts] == [OUTCOME_NOT_TRANSMITTED]
    assert attempts[0].finished_at is not None
    async with sqlite_session_factory() as session:
        assert await transmissions_on_day(session, profile_id=profile_id) == 0
    assert not await _daily_limit_failed(sqlite_session_factory, tmp_path, application_id)


async def test_unknown_provider_outcome_keeps_reservation(
    sqlite_session_factory, tmp_path: Path
) -> None:
    application_id, profile_id = await _approved_graph(sqlite_session_factory, tmp_path)
    service = EmailService(
        settings(tmp_path), sqlite_session_factory, FakeGmailProvider(failure_mode="unknown")
    )
    delivery = await service.send_application(application_id)

    assert delivery.status is DeliveryStatus.DELIVERY_UNKNOWN
    attempts = await _attempts(sqlite_session_factory, application_id)
    assert [item.outcome for item in attempts] == [OUTCOME_DELIVERY_UNKNOWN]
    async with sqlite_session_factory() as session:
        assert await transmissions_on_day(session, profile_id=profile_id) == 1


async def test_abandoned_in_flight_attempt_keeps_reservation_when_reconciled(
    sqlite_session_factory, tmp_path: Path
) -> None:
    application_id, profile_id = await _approved_graph(sqlite_session_factory, tmp_path)
    stale = datetime.now(UTC) - timedelta(hours=1)
    async with sqlite_session_factory() as session:
        application = await session.get(Application, application_id)
        assert application is not None
        application.status = ApplicationStatus.SENDING
        delivery = EmailDelivery(
            application_id=application_id,
            provider="fake_gmail",
            recipient="jobs@example.com",
            status=DeliveryStatus.SENDING,
            sanitized_provider_response={},
            last_attempt_at=stale,
            created_at=stale,
            updated_at=stale,
        )
        session.add(delivery)
        await session.flush()
        session.add(
            EmailSendAttempt(
                delivery_id=delivery.id,
                application_id=application_id,
                profile_id=profile_id,
                attempt_no=1,
                local_day=local_day_key(),
                outcome=OUTCOME_IN_FLIGHT,
                started_at=stale,
            )
        )
        await session.commit()
        assert await transmissions_on_day(session, profile_id=profile_id) == 1

    async with sqlite_session_factory() as session:
        await reconcile_stale_delivery_unknown(session, application_id, actor="test")
        await session.commit()

    attempts = await _attempts(sqlite_session_factory, application_id)
    assert [item.outcome for item in attempts] == [OUTCOME_DELIVERY_UNKNOWN]
    async with sqlite_session_factory() as session:
        assert await transmissions_on_day(session, profile_id=profile_id) == 1


async def test_retry_on_another_local_day_counts_on_the_actual_transmission_day(
    sqlite_session_factory, tmp_path: Path
) -> None:
    application_id, profile_id = await _approved_graph(sqlite_session_factory, tmp_path)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    async with sqlite_session_factory() as session:
        application = await session.get(Application, application_id)
        assert application is not None
        application.status = ApplicationStatus.FAILED
        application.sent_at = yesterday
        delivery = EmailDelivery(
            application_id=application_id,
            provider="fake_gmail",
            recipient="jobs@example.com",
            status=DeliveryStatus.BOUNCED_TRANSIENT,
            sanitized_provider_response={},
            attempt_count=1,
            submitted_at=yesterday,
            provider_accepted_at=yesterday,
            last_attempt_at=yesterday,
            created_at=yesterday,
            next_retry_at=yesterday,
        )
        session.add(delivery)
        await session.flush()
        session.add(
            EmailSendAttempt(
                delivery_id=delivery.id,
                application_id=application_id,
                profile_id=profile_id,
                attempt_no=1,
                local_day=local_day_key(yesterday),
                outcome=OUTCOME_PROVIDER_ACCEPTED,
                started_at=yesterday,
                finished_at=yesterday,
            )
        )
        await session.commit()
        # Yesterday's transmission does not consume today's maximum.
        assert await transmissions_on_day(session, profile_id=profile_id) == 0
        assert (
            await transmissions_on_day(session, profile_id=profile_id, day=local_day_key(yesterday))
            == 1
        )

    service = EmailService(settings(tmp_path), sqlite_session_factory, FakeGmailProvider())
    retried = await service.send_application(application_id)

    assert retried.status is DeliveryStatus.PROVIDER_ACCEPTED
    assert retried.attempt_count == 2
    attempts = await _attempts(sqlite_session_factory, application_id)
    assert [(item.attempt_no, item.local_day) for item in attempts] == [
        (1, local_day_key(yesterday)),
        (2, local_day_key()),
    ]
    async with sqlite_session_factory() as session:
        assert await transmissions_on_day(session, profile_id=profile_id) == 1
    assert await _daily_limit_failed(sqlite_session_factory, tmp_path, application_id)


def test_local_day_key_uses_europe_chisinau_calendar() -> None:
    # 21:30 UTC on 29 September is already 30 September in Chisinau (UTC+3).
    assert local_day_key(datetime(2026, 9, 29, 21, 30, tzinfo=UTC)) == "2026-09-30"
    assert local_day_key(datetime(2026, 9, 29, 20, 59, tzinfo=UTC)) == "2026-09-29"


async def test_report_keeps_the_score_that_authorized_a_send(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from app.applications.details import get_application_detail
    from app.models.entities import MatchEvaluation
    from app.models.enums import MatchDecision
    from app.reports.service import _generate

    application_id, _profile_id = await _approved_graph(sqlite_session_factory, tmp_path)
    service = EmailService(settings(tmp_path), sqlite_session_factory, FakeGmailProvider())
    await service.send_application(application_id)

    async with sqlite_session_factory() as session:
        application = await session.get(Application, application_id)
        assert application is not None
        bound = await session.get(MatchEvaluation, application.match_evaluation_id)
        assert bound is not None and bound.overall_fit == 92
        snapshot = application.policy_result["send_authorization"]
        assert snapshot["evaluation_id"] == str(bound.id)
        assert snapshot["overall_score"] == 92
        assert snapshot["effective_threshold"] == 80
        assert snapshot["authority"] == "automatic"
        assert snapshot["attempt_no"] == 1

        def reevaluation(score: int, decision: MatchDecision, risks: list[str], offset: int):
            values = {
                column.key: getattr(bound, column.key)
                for column in MatchEvaluation.__table__.columns
                if column.key != "id"
            }
            values.update(
                overall_fit=score,
                decision=decision,
                risks=risks,
                created_at=bound.created_at + timedelta(minutes=offset),
            )
            return MatchEvaluation(**values)

        session.add(
            reevaluation(
                0,
                MatchDecision.PREPARE_FOR_REVIEW,
                ["llm_provider_failure:llmrouter:schema_validation:literal_error"],
                1,
            )
        )
        await session.commit()
        report = await _generate(session, persist=False)
        [sent] = report.summary["sent_applications"]
        assert sent["overall_score"] == 92
        assert sent["overall_score_source"] == "bound_evaluation"
        assert sent["match_evaluation_id"] == str(bound.id)
        assert sent["latest_evaluation_score"] == 0
        assert sent["send_threshold"] == 80

        session.add(reevaluation(80, MatchDecision.SKIP, [], 2))
        await session.commit()
        report = await _generate(session, persist=False)
        [sent] = report.summary["sent_applications"]
        assert sent["overall_score"] == 92
        assert sent["latest_evaluation_score"] == 80

        detail = await get_application_detail(session, application_id)
        assert str(detail["match_evaluation"]["id"]) == str(bound.id)


async def test_report_marks_score_unknown_without_a_bound_evaluation(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from app.reports.service import _generate

    application_id, _profile_id = await _approved_graph(sqlite_session_factory, tmp_path)
    service = EmailService(settings(tmp_path), sqlite_session_factory, FakeGmailProvider())
    await service.send_application(application_id)
    async with sqlite_session_factory() as session:
        application = await session.get(Application, application_id)
        assert application is not None
        application.match_evaluation_id = None
        await session.commit()
        report = await _generate(session, persist=False)
        [sent] = report.summary["sent_applications"]
        assert sent["overall_score"] is None
        assert sent["overall_score_source"] == "unknown"
        assert sent["latest_evaluation_score"] == 92
