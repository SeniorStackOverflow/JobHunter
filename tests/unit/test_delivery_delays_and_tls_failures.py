"""A delayed or finally failed delivery is visible, never resent, and pauses the address.

Production case (2026-10-01): the recipient's mail server failed STARTTLS.
Gmail warned about the delay after a day, retried for three days and only then
gave up with "Action: failed" and a 4.7.0 status. JobHunter showed nothing
about the delay, and would have read the final notice as a transient bounce
and sent the same letter again, restarting another three-day retry window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contacts import (
    ContactDiscoveryService,
    failure_reason_after_pause,
    release_paused_email_contacts,
)
from app.email.delivery import (
    EmailDeliveryReconciliationService,
    MailboxMessage,
    classify_smtp_failure,
    delay_alert_code,
)
from app.models.entities import (
    Alert,
    Application,
    EmailDelivery,
    EmployerContact,
    SourceJob,
)
from app.models.enums import (
    ApplicationStatus,
    ContactDeliveryState,
    ContactType,
    DeliveryStatus,
    VerificationStatus,
)
from app.reports.service import _generate
from tests.unit.test_email_delivery_reconciliation import (
    StaticMailbox,
    _delivery_graph,
    _settings,
    _structured_dsn,
)

TLS_DIAGNOSTIC = "TLS Negotiation failed. Try again later."
RESENDABLE = {
    DeliveryStatus.TEMPORARY_FAILURE,
    DeliveryStatus.BOUNCED_TRANSIENT,
    DeliveryStatus.MAILBOX_FULL,
}


def _notice(message_id: str, *, action: str, status: str, diagnostic: str, when: datetime):
    return MailboxMessage(
        message_id=message_id,
        thread_id="thread-1",
        history_id=f"history-{message_id}",
        raw=_structured_dsn(action=action, status=status, diagnostic=diagnostic, when=when),
        inbox=True,
    )


async def _reconcile(session_factory, *messages: MailboxMessage) -> None:
    for message in messages:
        await EmailDeliveryReconciliationService(
            _settings(), session_factory, StaticMailbox(message)
        ).reconcile()


async def _graph_with_sibling(session_factory):
    """job@sincer.md received the application; hr@sincer.md is the same domain."""

    async with session_factory() as session:
        application, delivery, contact, employer = await _delivery_graph(session)
        sibling = EmployerContact(
            canonical_job_id=contact.canonical_job_id,
            employer_id=employer.id,
            source_job_id=contact.source_job_id,
            value="hr@sincer.md",
            contact_type=ContactType.EMAIL,
            discovery_source="fixture",
            verification_status=VerificationStatus.VERIFIED,
            confidence=1,
            evidence_url=contact.evidence_url,
        )
        session.add(sibling)
        await session.commit()
        return application.id, delivery.id, contact.id, sibling.id


# --- classification -------------------------------------------------------


def test_final_tls_failure_is_a_domain_failure_not_a_rate_limit() -> None:
    result = classify_smtp_failure(f"smtp; {TLS_DIAGNOSTIC}", status="4.7.0", action="failed")

    assert result.status is DeliveryStatus.DOMAIN_REJECTED
    assert result.failure_class == "tls_failure"
    assert result.retryable is False
    assert result.permanent is True


@pytest.mark.parametrize(
    ("status", "diagnostic", "failure_class", "delivery_status"),
    [
        ("4.7.0", "smtp; 421 4.7.0 Try again later", "delivery_expired", "bounced_permanent"),
        ("4.4.7", "smtp; connection timed out", "delivery_expired", "bounced_permanent"),
        ("4.2.2", "smtp; mailbox full", "mailbox_full", "bounced_permanent"),
    ],
)
def test_provider_giving_up_on_a_temporary_error_is_final(
    status: str, diagnostic: str, failure_class: str, delivery_status: str
) -> None:
    result = classify_smtp_failure(diagnostic, status=status, action="failed")

    assert result.failure_class == failure_class
    assert result.status.value == delivery_status
    assert result.retryable is False


def test_a_delay_warning_stays_accepted() -> None:
    result = classify_smtp_failure(f"smtp; {TLS_DIAGNOSTIC}", status="4.7.0", action="delayed")

    assert result.status is DeliveryStatus.PROVIDER_ACCEPTED
    assert result.failure_class == "delivery_delayed"
    assert result.retryable is False


def test_an_immediate_temporary_rejection_is_still_retried() -> None:
    """Without "Action: failed" nobody retried yet, so JobHunter may."""

    result = classify_smtp_failure("421 4.7.0 rate limit exceeded")

    assert result.status is DeliveryStatus.BOUNCED_TRANSIENT
    assert result.retryable is True


# --- reconciliation ---------------------------------------------------------


async def test_delay_notice_is_recorded_and_raises_one_warning(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    application_id, delivery_id, contact_id, _sibling = await _graph_with_sibling(
        sqlite_session_factory
    )
    when = datetime(2026, 10, 2, 16, 14, tzinfo=UTC)

    await _reconcile(
        sqlite_session_factory,
        _notice("delay-1", action="delayed", status="4.7.0", diagnostic=TLS_DIAGNOSTIC, when=when),
        _notice(
            "delay-2",
            action="delayed",
            status="4.7.0",
            diagnostic=TLS_DIAGNOSTIC,
            when=when + timedelta(hours=12),
        ),
    )

    async with sqlite_session_factory() as session:
        delivery = await session.get(EmailDelivery, delivery_id)
        application = await session.get(Application, application_id)
        contact = await session.get(EmployerContact, contact_id)
        alerts = list(
            (
                await session.scalars(
                    select(Alert).where(Alert.code == delay_alert_code(delivery_id))
                )
            ).all()
        )
        assert delivery is not None and application is not None and contact is not None
        assert delivery.status is DeliveryStatus.PROVIDER_ACCEPTED
        assert delivery.failure_class == "delivery_delayed"
        assert TLS_DIAGNOSTIC in (delivery.failure_reason or "")
        assert delivery.next_retry_at is None
        assert application.status is ApplicationStatus.SENT
        # The provider is still retrying: nothing is blocked yet.
        assert contact.delivery_state is ContactDeliveryState.UNKNOWN
        assert len(alerts) == 1
        assert alerts[0].acknowledged is False
        assert alerts[0].severity == "warning"


async def test_final_tls_failure_is_not_resent_and_pauses_the_domain(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    application_id, delivery_id, contact_id, sibling_id = await _graph_with_sibling(
        sqlite_session_factory
    )
    when = datetime(2026, 10, 2, 16, 14, tzinfo=UTC)

    await _reconcile(
        sqlite_session_factory,
        _notice("delay", action="delayed", status="4.7.0", diagnostic=TLS_DIAGNOSTIC, when=when),
        _notice(
            "final",
            action="failed",
            status="4.7.0",
            diagnostic=TLS_DIAGNOSTIC,
            when=when + timedelta(days=2),
        ),
    )

    async with sqlite_session_factory() as session:
        delivery = await session.get(EmailDelivery, delivery_id)
        application = await session.get(Application, application_id)
        contact = await session.get(EmployerContact, contact_id)
        sibling = await session.get(EmployerContact, sibling_id)
        delay_alert = await session.scalar(
            select(Alert).where(Alert.code == delay_alert_code(delivery_id))
        )
        failure_alert = await session.scalar(
            select(Alert).where(Alert.code == f"email_permanent_delivery_failure:{delivery_id}")
        )
        assert delivery is not None and application is not None
        assert delivery.status is DeliveryStatus.DOMAIN_REJECTED
        assert delivery.status not in RESENDABLE
        assert delivery.next_retry_at is None
        assert delivery.failure_class == "tls_failure"
        assert application.status is ApplicationStatus.FAILED
        assert application.recipient_contact_id == contact_id
        for paused in (contact, sibling):
            assert paused is not None
            assert paused.delivery_state is ContactDeliveryState.REJECTED
            assert paused.last_failure_reason == "tls_failure"
        assert delay_alert is not None and delay_alert.acknowledged is True
        assert delay_alert.safe_diagnostics["resolution"]["reason"] == "delivery_domain_rejected"
        assert failure_alert is not None
        # The other address shares the broken server, so it is no alternative.
        assert failure_alert.safe_diagnostics["alternate_recipient"] is None


async def test_final_delivery_answers_the_delay_warning(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _application_id, delivery_id, _contact_id, _sibling = await _graph_with_sibling(
        sqlite_session_factory
    )
    when = datetime(2026, 10, 2, 16, 14, tzinfo=UTC)

    await _reconcile(
        sqlite_session_factory,
        _notice("delay", action="delayed", status="4.7.0", diagnostic=TLS_DIAGNOSTIC, when=when),
        _notice(
            "delivered",
            action="delivered",
            status="2.0.0",
            diagnostic="250 2.0.0 OK",
            when=when + timedelta(hours=5),
        ),
    )

    async with sqlite_session_factory() as session:
        delivery = await session.get(EmailDelivery, delivery_id)
        alert = await session.scalar(
            select(Alert).where(Alert.code == delay_alert_code(delivery_id))
        )
        assert delivery is not None and delivery.status is DeliveryStatus.DELIVERED
        assert delivery.failure_class is None
        assert alert is not None and alert.acknowledged is True
        assert alert.safe_diagnostics["resolution"]["reason"] == "delivery_delivered"


# --- pause and release --------------------------------------------------------


async def _paused_contact(session, *, reason: str, days_ago: float) -> EmployerContact:
    _application, _delivery, contact, _employer = await _delivery_graph(session)
    contact.delivery_state = ContactDeliveryState.REJECTED
    contact.last_failure_reason = reason
    contact.last_delivery_failure_at = datetime.now(UTC) - timedelta(days=days_ago)
    await session.flush()
    return contact


@pytest.mark.parametrize(
    ("reason", "days_ago", "pause_days", "released"),
    [
        ("tls_failure", 15, 14, True),
        ("delivery_expired", 15, 14, True),
        ("mailbox_full", 15, 14, True),
        ("tls_failure", 13, 14, False),
        ("tls_failure", 200, 0, False),
        # A mailbox that does not exist is not a temporary condition.
        ("recipient_not_found", 200, 14, False),
        ("recipient_rejected", 200, 14, False),
    ],
)
async def test_pause_ends_only_for_temporary_conditions_after_the_window(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    reason: str,
    days_ago: float,
    pause_days: int,
    released: bool,
) -> None:
    async with sqlite_session_factory() as session:
        contact = await _paused_contact(session, reason=reason, days_ago=days_ago)

        count = await release_paused_email_contacts(
            session, now=datetime.now(UTC), pause_days=pause_days
        )

        assert count == int(released)
        assert contact.delivery_state is (
            ContactDeliveryState.TRANSIENT_FAILURE if released else ContactDeliveryState.REJECTED
        )
        assert contact.last_failure_reason == (f"pause_ended:{reason}" if released else reason)


async def test_periodic_reconciliation_releases_expired_pauses(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        contact = await _paused_contact(session, reason="tls_failure", days_ago=15)
        contact_id = contact.id
        await session.commit()

    await EmailDeliveryReconciliationService(
        _settings().model_copy(update={"gmail_delivery_reconciliation_enabled": True}),
        sqlite_session_factory,
        StaticMailbox(),
    ).reconcile_all()

    async with sqlite_session_factory() as session:
        refreshed = await session.get(EmployerContact, contact_id)
        assert refreshed is not None
        assert refreshed.delivery_state is ContactDeliveryState.TRANSIENT_FAILURE


@pytest.mark.parametrize(("days_ago", "inherited"), [(2, True), (15, False)])
async def test_a_new_address_of_a_paused_domain_waits_for_the_pause(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    days_ago: float,
    inherited: bool,
) -> None:
    async with sqlite_session_factory() as session:
        contact = await _paused_contact(session, reason="tls_failure", days_ago=days_ago)
        await release_paused_email_contacts(session, now=datetime.now(UTC), pause_days=14)
        job = await session.get(SourceJob, contact.source_job_id)
        assert job is not None
        job.public_email = "office@sincer.md"
        job.public_emails = ["office@sincer.md"]
        # The address appears for an employer the failure was never linked to.
        job.employer_id = None

        discovered = await ContactDiscoveryService().discover_email_contacts(session, job)

        new_contact = next(item for item in discovered if item.value == "office@sincer.md")
        assert (new_contact.delivery_state is ContactDeliveryState.REJECTED) is inherited


async def test_other_domains_are_not_paused(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        contact = await _paused_contact(session, reason="tls_failure", days_ago=1)
        job = await session.get(SourceJob, contact.source_job_id)
        assert job is not None
        job.public_email = "hr@notsincer.md"
        job.public_emails = ["hr@notsincer.md"]

        discovered = await ContactDiscoveryService().discover_email_contacts(session, job)

        assert discovered[0].delivery_state is ContactDeliveryState.UNKNOWN


# --- a failed probe after the pause is final ------------------------------------


async def _graph_after_pause(session_factory):
    """Both addresses of the domain were paused once and the pause has ended."""

    ids = await _graph_with_sibling(session_factory)
    async with session_factory() as session:
        for contact_id in ids[2:]:
            contact = await session.get(EmployerContact, contact_id)
            assert contact is not None
            contact.delivery_state = ContactDeliveryState.TRANSIENT_FAILURE
            contact.last_failure_reason = "pause_ended:tls_failure"
            contact.last_delivery_failure_at = datetime.now(UTC) - timedelta(days=20)
        await session.commit()
    return ids


async def test_tls_failure_again_after_the_pause_blocks_the_domain_for_good(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _application_id, _delivery_id, contact_id, sibling_id = await _graph_after_pause(
        sqlite_session_factory
    )

    await _reconcile(
        sqlite_session_factory,
        _notice(
            "final-again",
            action="failed",
            status="4.7.0",
            diagnostic=TLS_DIAGNOSTIC,
            when=datetime.now(UTC),
        ),
    )

    async with sqlite_session_factory() as session:
        released = await release_paused_email_contacts(
            session, now=datetime.now(UTC) + timedelta(days=365), pause_days=14
        )
        assert released == 0
        for contact_id_ in (contact_id, sibling_id):
            contact = await session.get(EmployerContact, contact_id_)
            assert contact is not None
            assert contact.delivery_state is ContactDeliveryState.REJECTED
            assert contact.last_failure_reason == "tls_failure_repeated"


async def test_a_new_address_fails_for_good_when_its_domain_already_had_a_pause(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        contact = await _paused_contact(session, reason="tls_failure", days_ago=20)
        contact.delivery_state = ContactDeliveryState.TRANSIENT_FAILURE
        contact.last_failure_reason = "pause_ended:tls_failure"
        newcomer = EmployerContact(
            canonical_job_id=contact.canonical_job_id,
            employer_id=contact.employer_id,
            source_job_id=contact.source_job_id,
            value="office@sincer.md",
            contact_type=ContactType.EMAIL,
            discovery_source="fixture",
            verification_status=VerificationStatus.VERIFIED,
            confidence=1,
            evidence_url=contact.evidence_url,
        )
        session.add(newcomer)
        await session.flush()

        reason = await failure_reason_after_pause(
            session, contact=newcomer, failure_class="tls_failure"
        )

        assert reason == "tls_failure_repeated"


@pytest.mark.parametrize(
    ("previous", "failure_class", "expected"),
    [
        (None, "tls_failure", "tls_failure"),
        (None, "delivery_expired", "delivery_expired"),
        ("pause_ended:delivery_expired", "delivery_expired", "delivery_expired_repeated"),
        ("pause_ended:mailbox_full", "delivery_expired", "delivery_expired_repeated"),
        # Not a pausing kind of failure: nothing to escalate.
        ("pause_ended:tls_failure", "recipient_not_found", "recipient_not_found"),
        # An ordinary earlier failure is not a pause that ended.
        ("rate_limited", "tls_failure", "tls_failure"),
    ],
)
async def test_only_a_failure_after_an_ended_pause_is_escalated(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    previous: str | None,
    failure_class: str,
    expected: str,
) -> None:
    async with sqlite_session_factory() as session:
        _application, _delivery, contact, _employer = await _delivery_graph(session)
        contact.last_failure_reason = previous

        reason = await failure_reason_after_pause(
            session, contact=contact, failure_class=failure_class
        )

        assert reason == expected


async def test_a_permanently_blocked_domain_also_blocks_new_addresses(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        contact = await _paused_contact(session, reason="tls_failure_repeated", days_ago=300)
        await release_paused_email_contacts(session, now=datetime.now(UTC), pause_days=14)
        job = await session.get(SourceJob, contact.source_job_id)
        assert job is not None
        job.public_email = "office@sincer.md"
        job.public_emails = ["office@sincer.md"]

        discovered = await ContactDiscoveryService().discover_email_contacts(session, job)

        new_contact = next(item for item in discovered if item.value == "office@sincer.md")
        assert new_contact.delivery_state is ContactDeliveryState.REJECTED


# --- panel and report ---------------------------------------------------------


def _detail(status: str, failure_class: str | None, reason: str = "", phones=None):
    return {
        "status": "failed" if status != "provider_accepted" else "sent",
        "delivery": {
            "status": status,
            "failure_class": failure_class,
            "failure_reason": reason,
            "final_recipient": "hr@citron.md",
        },
        "job": {"public_phone": (phones or [None])[0], "public_phones": phones or []},
    }


def test_panel_explains_a_tls_delay() -> None:
    from app.ui.presentation import _application_delivery_note

    note = _application_delivery_note(
        _detail("provider_accepted", "delivery_delayed", f"4.7.0 smtp; {TLS_DIAGNOSTIC}")
    )

    assert note is not None
    tone, text = note
    assert tone == "warning"
    assert "citron.md не может установить защищённое соединение" in text
    assert "повторно не отправит" in text


def test_panel_offers_the_vacancy_phone_after_a_final_tls_failure() -> None:
    from app.ui.presentation import _application_delivery_note

    note = _application_delivery_note(
        _detail("domain_rejected", "tls_failure", phones=["+37360000000"])
    )

    assert note is not None
    tone, text = note
    assert tone == "danger"
    assert "поставлен на паузу" in text
    assert "+37360000000" in text


def test_panel_says_a_repeated_tls_failure_is_final() -> None:
    from app.ui.presentation import _application_delivery_note

    detail = _detail("domain_rejected", "tls_failure")
    detail["contact"] = {"last_failure_reason": "tls_failure_repeated"}

    note = _application_delivery_note(detail)

    assert note is not None
    assert "больше не пишет" in note[1]
    assert "поставлен на паузу" not in note[1]


def test_panel_explains_an_expired_delivery() -> None:
    from app.ui.presentation import _application_delivery_note

    note = _application_delivery_note(_detail("bounced_permanent", "delivery_expired"))

    assert note is not None
    assert "прекратил попытки" in note[1]


@pytest.mark.parametrize(
    ("status", "failure_class"),
    [
        ("provider_accepted", None),
        ("delivered", None),
        ("recipient_rejected", "recipient_rejected"),
    ],
)
def test_panel_adds_no_delivery_note_otherwise(status: str, failure_class: str | None) -> None:
    from app.ui.presentation import _application_delivery_note

    assert _application_delivery_note(_detail(status, failure_class)) is None


def test_application_detail_template_shows_the_delivery_note() -> None:
    template = Path("app/admin/templates/application_detail.html").read_text(encoding="utf-8")

    assert "application_delivery_note(application)" in template


def test_delay_alert_has_a_readable_label() -> None:
    from app.ui.presentation import _alert_code_label

    assert _alert_code_label("email_delivery_delayed:abc") == "Доставка отклика задерживается"


async def test_daily_report_lists_deliveries_still_delayed(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        _application, delivery, _contact, _employer = await _delivery_graph(session)
        delivery.failure_class = "delivery_delayed"
        delivery.smtp_status = "4.7.0"

        report = await _generate(session)

        email = report.summary["email_delivery"]
        assert email["delayed_in_flight"] == 1
        assert email["delayed_recipients"] == [
            {"recipient": "job@sincer.md", "smtp_status": "4.7.0"}
        ]


@pytest.mark.parametrize(
    ("status", "label", "tone"),
    [
        ("provider_accepted", "Принято Gmail", "info"),
        ("delivered", "Доставлено", "success"),
        ("domain_rejected", "Домен не принимает письма", "danger"),
        ("bounced_permanent", "Не доставлено", "danger"),  # noqa: RUF001
        ("bounced_transient", "Временный отказ", "warning"),
    ],
)
def test_delivery_statuses_have_russian_labels(status: str, label: str, tone: str) -> None:
    from app.ui.presentation import _status_label, _status_tone

    assert _status_label(status) == label
    assert _status_tone(status) == tone
