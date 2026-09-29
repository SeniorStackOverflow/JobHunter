from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.email.delivery import (
    EmailDeliveryReconciliationService,
    GmailMailboxProvider,
    MailboxBatch,
    MailboxMessage,
    classify_smtp_failure,
    parse_delivery_notice,
)
from app.models.constants import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.models.entities import (
    Alert,
    Application,
    CanonicalEmployer,
    CanonicalJob,
    EmailDelivery,
    EmailDeliveryEvent,
    EmailMailboxCursor,
    EmployerContact,
    EmployerRelationship,
    JobSource,
    Resume,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    ContactDeliveryState,
    ContactType,
    DeliveryStatus,
    EmployerRelationshipState,
    JobStatus,
    VerificationStatus,
)
from app.reports.service import _generate
from app.settings import Settings


class StaticMailbox:
    def __init__(self, *messages: MailboxMessage) -> None:
        self.messages = messages

    async def fetch(
        self, *, start_history_id: str | None, max_results: int, monitor_days: int
    ) -> MailboxBatch:
        return MailboxBatch(self.messages, "history-2")


@pytest.mark.asyncio
async def test_stale_mailbox_batch_does_not_rewind_cursor(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        session.add(EmailMailboxCursor(provider="gmail", history_id="100"))
        await session.commit()

    class AdvancedMailbox:
        async def fetch(
            self, *, start_history_id: str | None, max_results: int, monitor_days: int
        ) -> MailboxBatch:
            assert start_history_id == "100"
            async with sqlite_session_factory() as session:
                cursor = await session.get(
                    EmailMailboxCursor,
                    (BOOTSTRAP_ADMIN_ACCOUNT_ID, "gmail"),
                )
                assert cursor is not None
                cursor.history_id = "200"
                await session.commit()
            return MailboxBatch((), "150")

    result = await EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, AdvancedMailbox()
    ).reconcile()

    async with sqlite_session_factory() as session:
        cursor = await session.get(EmailMailboxCursor, (BOOTSTRAP_ADMIN_ACCOUNT_ID, "gmail"))
        assert cursor is not None and cursor.history_id == "200"
    assert result["status"] == "ok"


class _FakeGmailApi:
    def __init__(self) -> None:
        self.action = ""
        self.history_start: str | None = None
        self.page_token: str | None = None
        self.message_id: str | None = None
        self.query: str | None = None

    def users(self) -> _FakeGmailApi:
        return self

    def history(self) -> _FakeGmailApi:
        return self

    def messages(self) -> _FakeGmailApi:
        return self

    def getProfile(self, **_kwargs: object) -> _FakeGmailApi:
        self.action = "profile"
        return self

    def list(self, **kwargs: object) -> _FakeGmailApi:
        self.action = "history" if "startHistoryId" in kwargs else "list"
        self.history_start = kwargs.get("startHistoryId")  # type: ignore[assignment]
        self.page_token = kwargs.get("pageToken")  # type: ignore[assignment]
        self.query = kwargs.get("q")  # type: ignore[assignment]
        return self

    def get(self, **kwargs: object) -> _FakeGmailApi:
        self.action = "message"
        self.message_id = kwargs.get("id")  # type: ignore[assignment]
        return self

    def execute(self) -> dict[str, object]:
        if self.action == "profile":
            return {"historyId": "104"}
        if self.action == "history":
            if self.history_start == "100":
                return {
                    "history": [
                        {
                            "id": "101",
                            "messagesAdded": [
                                {"message": {"id": item, "threadId": "thread"}}
                                for item in ("a", "b", "c")
                            ],
                        },
                        {
                            "id": "102",
                            "messagesAdded": [{"message": {"id": "d", "threadId": "thread"}}],
                        },
                    ]
                }
            return {
                "history": [
                    {
                        "id": "102",
                        "messagesAdded": [{"message": {"id": "d", "threadId": "thread"}}],
                    }
                ]
            }
        if self.action == "list":
            if self.page_token == "next":
                return {"messages": [{"id": "b", "threadId": "thread"}]}
            if self.query and "in:inbox" in self.query:
                return {"messages": [{"id": "a", "threadId": "thread"}]}
            return {
                "messages": [{"id": "a", "threadId": "thread"}],
                "nextPageToken": "next",
            }
        assert self.message_id is not None
        return {
            "raw": base64.urlsafe_b64encode(b"From: sender@example.test\r\n\r\nbody").decode(),
            "threadId": "thread",
            "labelIds": ["INBOX"],
        }


@pytest.mark.asyncio
async def test_gmail_history_batch_keeps_every_message_in_boundary_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGmailApi()
    monkeypatch.setattr("app.email.delivery.build", lambda *_args, **_kwargs: fake)
    provider = GmailMailboxProvider(
        client_id="fixture", client_secret="fixture", refresh_token="fixture"
    )

    first = await provider.fetch(start_history_id="100", max_results=2, monitor_days=14)
    second = await provider.fetch(
        start_history_id=first.next_history_id, max_results=2, monitor_days=14
    )

    assert [message.message_id for message in first.messages] == ["a", "b", "c"]
    assert first.next_history_id == "101"
    assert [message.message_id for message in second.messages] == ["d"]


@pytest.mark.asyncio
async def test_gmail_initial_scan_paginates_before_advancing_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGmailApi()
    monkeypatch.setattr("app.email.delivery.build", lambda *_args, **_kwargs: fake)
    provider = GmailMailboxProvider(
        client_id="fixture", client_secret="fixture", refresh_token="fixture"
    )

    batch = await provider.fetch(start_history_id=None, max_results=1, monitor_days=14)

    assert [message.message_id for message in batch.messages] == ["a", "b"]
    assert batch.next_history_id == "104"


def _settings(*, attempts: int = 4) -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        email_delivery_max_attempts=attempts,
    )


def _dsn(
    *,
    original_message_id: str,
    recipient: str,
    diagnostic: str,
    when: datetime,
) -> bytes:
    message = EmailMessage()
    message["From"] = "Mail Delivery Subsystem <mailer-daemon@example.net>"
    message["To"] = "candidate@example.test"
    message["Subject"] = "Delivery Status Notification (Failure)"
    message["Date"] = when.strftime("%a, %d %b %Y %H:%M:%S +0000")
    message.set_content(
        f"Original-Message-ID: {original_message_id}\n"
        f"Final-Recipient: rfc822; {recipient}\n"
        f"Diagnostic-Code: smtp; {diagnostic}\n"
    )
    return message.as_bytes()


def _structured_dsn(*, action: str, status: str, diagnostic: str, when: datetime) -> bytes:
    return (
        "From: MAILER-DAEMON@example.net\r\n"
        "Subject: Delivery Status Notification\r\n"
        f"Date: {when.strftime('%a, %d %b %Y %H:%M:%S +0000')}\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/report; report-type=delivery-status; boundary=dsn\r\n\r\n"
        "--dsn\r\nContent-Type: text/plain\r\n\r\nDelivery status update.\r\n"
        "--dsn\r\nContent-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; mx.example.net\r\n\r\n"
        "Final-Recipient: rfc822; job@sincer.md\r\n"
        f"Action: {action}\r\nStatus: {status}\r\n"
        f"Diagnostic-Code: smtp; {diagnostic}\r\n"
        "Original-Message-ID: <application-fixture@job-agent.invalid>\r\n\r\n"
        "--dsn--\r\n"
    ).encode()


async def _delivery_graph(
    session: AsyncSession, *, attempts: int = 1
) -> tuple[Application, EmailDelivery, EmployerContact, CanonicalEmployer]:
    profile = UserProfile(name="Candidate", is_default=True)
    employer = CanonicalEmployer(normalized_name="sincer", primary_domain="sincer.md")
    source = JobSource(
        name="Fixture",
        base_url="https://jobs.example",
        adapter_type="fixture_source",
        configuration={},
    )
    session.add_all([profile, employer, source])
    await session.flush()
    canonical = CanonicalJob(
        normalized_company="sincer",
        normalized_title="operator",
        normalized_location="",
        employer_id=employer.id,
        canonical_fingerprint="a" * 64,
        status=JobStatus.ACTIVE,
    )
    resume = Resume(
        profile_id=profile.id,
        name="Resume",
        category="general",
        storage_key="fixture.pdf",
        original_filename="fixture.pdf",
        mime_type="application/pdf",
        sha256="b" * 64,
        active=True,
        verified=True,
    )
    session.add_all([canonical, resume])
    await session.flush()
    job = SourceJob(
        source_id=source.id,
        canonical_job_id=canonical.id,
        employer_id=employer.id,
        external_job_id="sincer-1",
        canonical_url="https://jobs.example/sincer-1",
        localized_urls={},
        title="Operator",
        company="Sincer",
        categories_seen=[],
        cities=[],
        public_email="job@sincer.md",
        public_emails=["job@sincer.md", "hr@sincer.md"],
        content_hash="c" * 64,
        matching_content_hash="c" * 64,
        source_fingerprint="c" * 64,
        status=JobStatus.ACTIVE,
        raw_metadata={},
    )
    session.add(job)
    await session.flush()
    contact = EmployerContact(
        canonical_job_id=canonical.id,
        employer_id=employer.id,
        source_job_id=job.id,
        value="job@sincer.md",
        contact_type=ContactType.EMAIL,
        discovery_source="fixture",
        verification_status=VerificationStatus.VERIFIED,
        confidence=1,
        evidence_url=job.canonical_url,
    )
    session.add(contact)
    await session.flush()
    application = Application(
        profile_id=profile.id,
        canonical_job_id=canonical.id,
        employer_id=employer.id,
        source_job_id=job.id,
        resume_id=resume.id,
        recipient_contact_id=contact.id,
        subject="Application",
        body="Body",
        language="en",
        status=ApplicationStatus.SENT,
        idempotency_key="d" * 64,
        sent_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    session.add(application)
    await session.flush()
    delivery = EmailDelivery(
        application_id=application.id,
        provider="gmail",
        recipient=contact.value,
        provider_message_id="gmail-outbound-1",
        thread_id="thread-1",
        rfc_message_id="<application-fixture@job-agent.invalid>",
        status=DeliveryStatus.PROVIDER_ACCEPTED,
        sanitized_provider_response={},
        attempt_count=attempts,
        provider_accepted_at=application.sent_at,
        last_attempt_at=application.sent_at,
    )
    session.add(delivery)
    await session.commit()
    return application, delivery, contact, employer


@pytest.mark.parametrize(
    ("diagnostic", "status", "failure_class", "permanent", "retryable"),
    [
        (
            "550 5.1.1 User unknown",
            DeliveryStatus.RECIPIENT_REJECTED,
            "recipient_not_found",
            True,
            False,
        ),
        (
            "550 5.4.1 Recipient address rejected: Access denied.",
            DeliveryStatus.RECIPIENT_REJECTED,
            "recipient_rejected",
            True,
            False,
        ),
        (
            "421 4.4.2 Remote server unavailable",
            DeliveryStatus.BOUNCED_TRANSIENT,
            "remote_server_unavailable",
            False,
            True,
        ),
        (
            "452 4.2.2 Mailbox full",
            DeliveryStatus.MAILBOX_FULL,
            "mailbox_full",
            False,
            True,
        ),
        (
            "451 4.7.1 Recipient address rejected: temporary policy block",
            DeliveryStatus.BOUNCED_TRANSIENT,
            "recipient_rejected",
            False,
            True,
        ),
        (
            "451 4.7.1 Spam policy temporarily unavailable",
            DeliveryStatus.BOUNCED_TRANSIENT,
            "spam_policy",
            False,
            True,
        ),
    ],
)
def test_smtp_classifier(
    diagnostic: str,
    status: DeliveryStatus,
    failure_class: str,
    permanent: bool,
    retryable: bool,
) -> None:
    result = classify_smtp_failure(diagnostic)
    assert result.status is status
    assert result.failure_class == failure_class
    assert result.permanent is permanent
    assert result.retryable is retryable


def test_real_world_no_such_person_beats_generic_5_7_0() -> None:
    classification = classify_smtp_failure(
        'smtp; 550 No such person at this address."',
        status="5.7.0",
        action="failed",
    )

    assert classification.status is DeliveryStatus.RECIPIENT_REJECTED
    assert classification.failure_class == "recipient_not_found"
    assert classification.permanent is True
    assert classification.retryable is False
    assert classification.smtp_status == "550 5.7.0"


def test_mail_ru_disabled_mailbox_is_not_authentication_failure() -> None:
    classification = classify_smtp_failure(
        (
            "smtp; 550 Message was not accepted -- invalid mailbox. "
            "Local mailbox hr.draft@mail.ru is unavailable: account is disabled"
        ),
        status="5.7.0",
        action="failed",
    )

    assert classification.status is DeliveryStatus.RECIPIENT_REJECTED
    assert classification.failure_class == "recipient_not_found"
    assert classification.permanent is True
    assert classification.retryable is False


def test_structured_dsn_fields_are_parsed() -> None:
    raw = (
        b"From: MAILER-DAEMON@example.net\r\n"
        b"Subject: Delivery Status Notification\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/report; report-type=delivery-status; boundary=dsn\r\n\r\n"
        b"--dsn\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
        b"--dsn\r\nContent-Type: message/delivery-status\r\n\r\n"
        b"Reporting-MTA: dns; mx.example.net\r\n\r\n"
        b"Final-Recipient: rfc822; job@sincer.md\r\n"
        b"Action: failed\r\nStatus: 5.4.1\r\n"
        b"Diagnostic-Code: smtp; 550 5.4.1 Recipient address rejected: Access denied\r\n"
        b"Original-Message-ID: <application-fixture@job-agent.invalid>\r\n\r\n"
        b"--dsn--\r\n"
    )
    parsed = parse_delivery_notice(raw)
    assert parsed.is_dsn is True
    assert parsed.final_recipient == "job@sincer.md"
    assert parsed.original_message_id == "<application-fixture@job-agent.invalid>"
    assert parsed.status == "5.4.1"
    assert parsed.reporting_mta == "dns; mx.example.net"
    assert parsed.structured is True


@pytest.mark.asyncio
async def test_structured_dsn_does_not_correlate_from_human_readable_prose(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        _application, delivery, _contact, _employer = await _delivery_graph(session)
        delivery_id = delivery.id
    raw = (
        b"From: MAILER-DAEMON@example.net\r\n"
        b"Subject: Delivery Status Notification\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/report; report-type=delivery-status; boundary=dsn\r\n\r\n"
        b"--dsn\r\nContent-Type: text/plain\r\n\r\n"
        b"Original-Message-ID: <application-fixture@job-agent.invalid>\r\n"
        b"Final-Recipient: rfc822; job@sincer.md\r\n"
        b"Diagnostic-Code: smtp; 550 5.1.1 User unknown\r\n"
        b"--dsn\r\nContent-Type: message/delivery-status\r\n\r\n"
        b"Reporting-MTA: dns; mx.example.net\r\n\r\n"
        b"Action: failed\r\nStatus: 4.4.1\r\n\r\n"
        b"--dsn--\r\n"
    )
    notice = parse_delivery_notice(raw)
    assert notice.structured is True
    assert notice.original_message_id is None
    assert notice.final_recipient is None
    assert notice.diagnostic == ""
    assert notice.status == "4.4.1"

    result = await EmailDeliveryReconciliationService(
        _settings(),
        sqlite_session_factory,
        StaticMailbox(MailboxMessage("gmail-prose-only", "thread-1", "dsn-prose", raw, True)),
    ).reconcile()
    assert result["correlated"] == 0
    async with sqlite_session_factory() as session:
        refreshed = await session.get(EmailDelivery, delivery_id)
        event = await session.scalar(select(EmailDeliveryEvent))
        assert refreshed is not None and refreshed.status is DeliveryStatus.PROVIDER_ACCEPTED
        assert event is not None and event.delivery_id is None
        assert event.smtp_status == "4.4.1"


@pytest.mark.parametrize(
    ("action", "expected_status", "retryable"),
    [
        ("failed", DeliveryStatus.BOUNCED_TRANSIENT, True),
        ("delayed", DeliveryStatus.PROVIDER_ACCEPTED, False),
    ],
)
def test_structured_status_overrides_unrelated_diagnostic_number(
    action: str, expected_status: DeliveryStatus, retryable: bool
) -> None:
    result = classify_smtp_failure("smtp; 201 4.4.1 remote timeout", status="4.4.1", action=action)
    assert result.status is expected_status
    assert result.smtp_status == "4.4.1"
    assert result.retryable is retryable


def test_bare_positive_smtp_code_does_not_confirm_delivery() -> None:
    result = classify_smtp_failure("250 2.0.0 delivered")
    assert result.status is DeliveryStatus.PROVIDER_ACCEPTED
    assert result.retryable is False


@pytest.mark.asyncio
async def test_sincer_bounce_correlates_updates_contact_and_is_idempotent(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        application, delivery, contact, employer = await _delivery_graph(session)
        alternate = EmployerContact(
            canonical_job_id=contact.canonical_job_id,
            employer_id=employer.id,
            source_job_id=contact.source_job_id,
            value="hr@sincer.md",
            contact_type=ContactType.EMAIL,
            discovery_source="fixture",
            verification_status=VerificationStatus.VERIFIED,
            confidence=1,
            evidence_url="https://jobs.example/sincer-1",
        )
        session.add(alternate)
        await session.commit()
        application_id = application.id
        delivery_id = delivery.id
        contact_id = contact.id
        alternate_id = alternate.id
        employer_id = employer.id
    bounce_at = datetime(2026, 9, 21, 8, 17, tzinfo=UTC)
    message = MailboxMessage(
        message_id="gmail-bounce-1",
        thread_id="unrelated-bounce-thread",
        history_id="2",
        raw=_dsn(
            original_message_id="<application-fixture@job-agent.invalid>",
            recipient="job@sincer.md",
            diagnostic="550 5.4.1 Recipient address rejected: Access denied.",
            when=bounce_at,
        ),
        inbox=True,
    )
    service = EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, StaticMailbox(message)
    )

    first = await service.reconcile()
    second = await service.reconcile()
    assert first["correlated"] == 1
    assert second["processed"] == 0

    async with sqlite_session_factory() as session:
        refreshed_delivery = await session.get(EmailDelivery, delivery_id)
        refreshed_contact = await session.get(EmployerContact, contact_id)
        refreshed_application = await session.get(Application, application_id)
        alternate_contact = await session.get(EmployerContact, alternate_id)
        relationship = await session.scalar(
            select(EmployerRelationship).where(EmployerRelationship.employer_id == employer_id)
        )
        assert refreshed_delivery is not None
        assert refreshed_delivery.status is DeliveryStatus.RECIPIENT_REJECTED
        assert refreshed_delivery.next_retry_at is not None
        assert refreshed_delivery.bounced_at == bounce_at.replace(tzinfo=None)
        assert refreshed_delivery.sanitized_provider_response["recipient_fallback_pending"] is True
        assert refreshed_delivery.sanitized_provider_response["recipient_fallback_used"] is False
        assert (
            refreshed_delivery.sanitized_provider_response["recipient_fallback_from"]
            == "job@sincer.md"
        )
        assert (
            refreshed_delivery.sanitized_provider_response["recipient_fallback_to"]
            == "hr@sincer.md"
        )
        assert refreshed_contact is not None
        assert refreshed_contact.delivery_state is ContactDeliveryState.REJECTED
        assert refreshed_contact.failure_count == 1
        assert refreshed_application is not None
        assert refreshed_application.status is ApplicationStatus.FAILED
        assert refreshed_application.recipient_contact_id == alternate_id
        assert alternate_contact is not None
        assert alternate_contact.delivery_state is ContactDeliveryState.UNKNOWN
        alert = await session.scalar(
            select(Alert).where(Alert.code == f"email_permanent_delivery_failure:{delivery_id}")
        )
        assert alert is not None
        assert alert.severity == "warning"
        assert alert.safe_diagnostics["alternate_recipient"] == "hr@sincer.md"
        assert alert.safe_diagnostics["failure_class"] == "recipient_rejected"
        assert alert.safe_diagnostics["fallback_scheduled"] is True
        assert await session.scalar(select(func.count(EmailDelivery.id))) == 1
        assert relationship is not None
        assert relationship.state is EmployerRelationshipState.NEVER_CONTACTED
        assert relationship.suppression_scope.value == "none"
        assert await session.scalar(select(func.count(EmailDeliveryEvent.id))) == 1


@pytest.mark.asyncio
async def test_permanent_bounce_never_schedules_a_second_recipient_fallback(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        application, delivery, contact, employer = await _delivery_graph(session)
        alternate = EmployerContact(
            canonical_job_id=contact.canonical_job_id,
            employer_id=employer.id,
            source_job_id=contact.source_job_id,
            value="hr@sincer.md",
            contact_type=ContactType.EMAIL,
            discovery_source="job_detail_explicit_email",
            verification_status=VerificationStatus.SOURCE_VERIFIED,
            confidence=1,
            evidence_url="https://jobs.example/sincer-1",
        )
        session.add(alternate)
        delivery.sanitized_provider_response = {
            "recipient_fallback_used": True,
            "recipient_fallback_pending": False,
        }
        await session.commit()
        application_id = application.id
        delivery_id = delivery.id
        contact_id = contact.id

    when = datetime(2026, 9, 21, 8, 30, tzinfo=UTC)
    message = MailboxMessage(
        message_id="gmail-bounce-after-fallback",
        thread_id="thread-1",
        history_id="fallback-used-history",
        raw=_dsn(
            original_message_id="<application-fixture@job-agent.invalid>",
            recipient="job@sincer.md",
            diagnostic="550 5.1.1 User unknown",
            when=when,
        ),
        inbox=True,
    )
    await EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, StaticMailbox(message)
    ).reconcile()

    async with sqlite_session_factory() as session:
        refreshed_delivery = await session.get(EmailDelivery, delivery_id)
        refreshed_application = await session.get(Application, application_id)
        alert = await session.scalar(
            select(Alert).where(Alert.code == f"email_permanent_delivery_failure:{delivery_id}")
        )
        assert refreshed_delivery is not None
        assert refreshed_delivery.next_retry_at is None
        assert refreshed_delivery.sanitized_provider_response["recipient_fallback_used"] is True
        assert refreshed_application is not None
        assert refreshed_application.recipient_contact_id == contact_id
        assert refreshed_application.status is ApplicationStatus.FAILED
        assert alert is not None
        assert alert.safe_diagnostics["fallback_scheduled"] is False


@pytest.mark.asyncio
async def test_transient_bounce_schedules_bounded_retry_without_invalidating_contact(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        application, delivery, contact, _employer = await _delivery_graph(session)
        delivery_id = delivery.id
        contact_id = contact.id
        application_id = application.id
    when = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
    message = MailboxMessage(
        message_id="gmail-bounce-transient",
        thread_id=None,
        history_id="3",
        raw=_dsn(
            original_message_id="<application-fixture@job-agent.invalid>",
            recipient="job@sincer.md",
            diagnostic="421 4.4.2 Remote server unavailable",
            when=when,
        ),
        inbox=True,
    )
    await EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, StaticMailbox(message)
    ).reconcile()

    async with sqlite_session_factory() as session:
        refreshed = await session.get(EmailDelivery, delivery_id)
        refreshed_contact = await session.get(EmployerContact, contact_id)
        refreshed_application = await session.get(Application, application_id)
        assert refreshed is not None
        assert refreshed.status is DeliveryStatus.BOUNCED_TRANSIENT
        assert refreshed.next_retry_at == (when + timedelta(minutes=15)).replace(tzinfo=None)
        assert refreshed_contact is not None
        assert refreshed_contact.delivery_state is ContactDeliveryState.TRANSIENT_FAILURE
        assert refreshed_application is not None
        assert refreshed_application.status is ApplicationStatus.FAILED


@pytest.mark.asyncio
async def test_transient_bounce_at_max_attempts_has_no_retry(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        _application, delivery, contact, _employer = await _delivery_graph(session, attempts=4)
        delivery_id = delivery.id
        contact_id = contact.id
    message = MailboxMessage(
        message_id="gmail-bounce-max",
        thread_id=None,
        history_id="4",
        raw=_dsn(
            original_message_id="<application-fixture@job-agent.invalid>",
            recipient="job@sincer.md",
            diagnostic="421 4.4.2 Remote server unavailable",
            when=datetime(2026, 9, 21, 10, 0, tzinfo=UTC),
        ),
        inbox=True,
    )
    await EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, StaticMailbox(message)
    ).reconcile()
    async with sqlite_session_factory() as session:
        refreshed = await session.get(EmailDelivery, delivery_id)
        refreshed_contact = await session.get(EmployerContact, contact_id)
        assert refreshed is not None and refreshed.next_retry_at is None
        assert refreshed_contact is not None
        assert refreshed_contact.delivery_state is ContactDeliveryState.TRANSIENT_FAILURE


@pytest.mark.asyncio
async def test_unrelated_mailer_daemon_message_is_not_linked(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        _application, delivery, _contact, _employer = await _delivery_graph(session)
        delivery_id = delivery.id
    message = MailboxMessage(
        message_id="gmail-unrelated",
        thread_id="other-thread",
        history_id="5",
        raw=_dsn(
            original_message_id="<someone-else@example.test>",
            recipient="other@example.test",
            diagnostic="550 5.1.1 User unknown",
            when=datetime(2026, 9, 21, 11, 0, tzinfo=UTC),
        ),
        inbox=True,
    )
    result = await EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, StaticMailbox(message)
    ).reconcile()
    assert result["correlated"] == 0
    async with sqlite_session_factory() as session:
        refreshed = await session.get(EmailDelivery, delivery_id)
        event = await session.scalar(select(EmailDeliveryEvent))
        assert refreshed is not None
        assert refreshed.status is DeliveryStatus.PROVIDER_ACCEPTED
        assert event is not None and event.delivery_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["thread", "recipient"])
async def test_dsn_fallback_correlation_requires_a_unique_match(
    sqlite_session_factory: async_sessionmaker[AsyncSession], fallback: str
) -> None:
    async with sqlite_session_factory() as session:
        _application, delivery, _contact, _employer = await _delivery_graph(session)
        delivery_id = delivery.id
    when = datetime.now(UTC) + timedelta(minutes=1)
    message = MailboxMessage(
        message_id=f"gmail-{fallback}-fallback",
        thread_id="thread-1" if fallback == "thread" else None,
        history_id="fallback-history",
        raw=_dsn(
            original_message_id="<unknown@job-agent.invalid>",
            recipient="job@sincer.md",
            diagnostic="550 5.1.1 User unknown",
            when=when,
        ),
        inbox=True,
    )
    result = await EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, StaticMailbox(message)
    ).reconcile()

    assert result["correlated"] == 1
    async with sqlite_session_factory() as session:
        refreshed = await session.get(EmailDelivery, delivery_id)
        assert refreshed is not None
        assert refreshed.status is DeliveryStatus.RECIPIENT_REJECTED


@pytest.mark.asyncio
async def test_thread_fallback_rejects_conflicting_final_recipient(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        _application, delivery, _contact, _employer = await _delivery_graph(session)
        delivery_id = delivery.id
    message = MailboxMessage(
        message_id="gmail-wrong-thread-recipient",
        thread_id="thread-1",
        history_id="conflict-history",
        raw=_dsn(
            original_message_id="<unknown@job-agent.invalid>",
            recipient="someone-else@example.test",
            diagnostic="550 5.1.1 User unknown",
            when=datetime.now(UTC),
        ),
        inbox=True,
    )

    result = await EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, StaticMailbox(message)
    ).reconcile()

    assert result["correlated"] == 0
    async with sqlite_session_factory() as session:
        refreshed = await session.get(EmailDelivery, delivery_id)
        assert refreshed is not None
        assert refreshed.status is DeliveryStatus.PROVIDER_ACCEPTED


@pytest.mark.asyncio
async def test_late_positive_notice_cannot_revive_permanently_rejected_recipient(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        _application, delivery, contact, _employer = await _delivery_graph(session)
        delivery.status = DeliveryStatus.RECIPIENT_REJECTED
        delivery.bounced_at = datetime(2026, 9, 21, tzinfo=UTC)
        contact.delivery_state = ContactDeliveryState.INVALID
        delivery_id = delivery.id
        contact_id = contact.id
        await session.commit()
    message = MailboxMessage(
        message_id="gmail-late-positive",
        thread_id="thread-1",
        history_id="late-positive-history",
        raw=_structured_dsn(
            action="delivered",
            status="2.0.0",
            diagnostic="250 2.0.0 delivered",
            when=datetime(2026, 9, 20, tzinfo=UTC),
        ),
        inbox=True,
    )

    await EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, StaticMailbox(message)
    ).reconcile()

    async with sqlite_session_factory() as session:
        refreshed = await session.get(EmailDelivery, delivery_id)
        refreshed_contact = await session.get(EmployerContact, contact_id)
        event = await session.scalar(select(EmailDeliveryEvent))
        assert refreshed is not None and refreshed.status is DeliveryStatus.RECIPIENT_REJECTED
        assert refreshed_contact is not None
        assert refreshed_contact.delivery_state is ContactDeliveryState.INVALID
        assert event is not None
        assert event.safe_metadata["ignored_reason"] == "terminal_delivery_outcome_preserved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_status", "expected_contact_state", "retry_scheduled"),
    [
        ("failed", DeliveryStatus.BOUNCED_TRANSIENT, ContactDeliveryState.TRANSIENT_FAILURE, True),
        ("delayed", DeliveryStatus.PROVIDER_ACCEPTED, ContactDeliveryState.UNKNOWN, False),
    ],
)
async def test_structured_dsn_reconciliation_preserves_action_semantics(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    action: str,
    expected_status: DeliveryStatus,
    expected_contact_state: ContactDeliveryState,
    retry_scheduled: bool,
) -> None:
    async with sqlite_session_factory() as session:
        application, delivery, contact, _employer = await _delivery_graph(session)
        application_id, delivery_id, contact_id = application.id, delivery.id, contact.id
    when = datetime(2026, 9, 21, 9, tzinfo=UTC)
    message = MailboxMessage(
        message_id=f"gmail-{action}-201-4.4.1",
        thread_id="thread-1",
        history_id="dsn-history",
        raw=_structured_dsn(
            action=action,
            status="4.4.1",
            diagnostic="201 4.4.1 remote timeout",
            when=when,
        ),
        inbox=True,
    )
    service = EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, StaticMailbox(message)
    )
    audit = await service.audit_mailbox()
    assert audit["matches"][0]["proposed_delivery_status"] == expected_status.value
    assert audit["matches"][0]["retryable"] is retry_scheduled
    assert audit["matches"][0]["action"] == action

    first = await service.reconcile()
    second = await service.reconcile()
    assert first["correlated"] == 1
    assert second["processed"] == 0
    async with sqlite_session_factory() as session:
        refreshed_delivery = await session.get(EmailDelivery, delivery_id)
        refreshed_contact = await session.get(EmployerContact, contact_id)
        refreshed_application = await session.get(Application, application_id)
        events = (await session.scalars(select(EmailDeliveryEvent))).all()
        assert refreshed_delivery is not None
        assert refreshed_delivery.status is expected_status
        assert (refreshed_delivery.next_retry_at is not None) is retry_scheduled
        assert refreshed_contact is not None
        assert refreshed_contact.delivery_state is expected_contact_state
        assert refreshed_application is not None
        assert refreshed_application.status is (
            ApplicationStatus.FAILED if retry_scheduled else ApplicationStatus.SENT
        )
        assert len(events) == 1
        assert events[0].event_type == ("bounce" if retry_scheduled else "delivery_notice")
        assert events[0].smtp_status == "4.4.1"
        assert events[0].safe_metadata["structured"] is True


@pytest.mark.asyncio
async def test_positive_delivery_confirmation_marks_contact_healthy(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        _application, delivery, contact, _employer = await _delivery_graph(session)
        contact.delivery_state = ContactDeliveryState.TRANSIENT_FAILURE
        delivery_id = delivery.id
        contact_id = contact.id
        await session.commit()
    when = datetime.now(UTC)
    message = MailboxMessage(
        message_id="gmail-delivered",
        thread_id="thread-1",
        history_id="delivered-history",
        raw=_structured_dsn(
            action="delivered",
            status="2.0.0",
            diagnostic="250 2.0.0 delivered",
            when=when,
        ),
        inbox=True,
    )
    await EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, StaticMailbox(message)
    ).reconcile()

    async with sqlite_session_factory() as session:
        refreshed = await session.get(EmailDelivery, delivery_id)
        refreshed_contact = await session.get(EmployerContact, contact_id)
        assert refreshed is not None and refreshed.status is DeliveryStatus.DELIVERED
        assert refreshed_contact is not None
        assert refreshed_contact.delivery_state is ContactDeliveryState.HEALTHY


@pytest.mark.asyncio
async def test_inbox_reply_correlates_by_thread_and_freezes_employer(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        _application, _delivery, _contact, employer = await _delivery_graph(session)
        employer_id = employer.id
    reply = EmailMessage()
    reply["From"] = "Recruiter <hr@sincer.md>"
    reply["To"] = "candidate@example.test"
    reply["Subject"] = "Re: Application"
    reply["Date"] = "Mon, 21 Sep 2026 12:00:00 +0000"
    reply.set_content("Thank you. Let us schedule a call.")
    message = MailboxMessage(
        message_id="gmail-reply-1",
        thread_id="thread-1",
        history_id="6",
        raw=reply.as_bytes(),
        inbox=True,
    )
    result = await EmailDeliveryReconciliationService(
        _settings(), sqlite_session_factory, StaticMailbox(message)
    ).reconcile()
    assert result["correlated"] == 1
    async with sqlite_session_factory() as session:
        relationship = await session.scalar(
            select(EmployerRelationship).where(EmployerRelationship.employer_id == employer_id)
        )
        assert relationship is not None
        assert relationship.state is EmployerRelationshipState.EMPLOYER_REPLIED


@pytest.mark.asyncio
async def test_daily_report_has_smtp_failure_breakdown(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        _application, delivery, _contact, _employer = await _delivery_graph(session)
        now = datetime.now(UTC)
        delivery.status = DeliveryStatus.RECIPIENT_REJECTED
        delivery.submitted_at = now
        delivery.provider_accepted_at = now
        delivery.bounced_at = now
        delivery.smtp_status = "550 5.4.1"
        delivery.failure_class = "recipient_rejected"
        report = await _generate(session)

        assert report.summary["email_delivery"]["submitted"] == 1
        assert report.summary["email_delivery"]["provider_accepted"] == 1
        assert report.summary["email_delivery"]["bounced_permanent"] == 1
        assert report.summary["email_delivery"]["permanent_failure_breakdown"] == {
            "550 5.4.1:recipient_rejected": 1
        }
        assert report.summary["email_delivery"]["alerts"] == []
