from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.contacts import propagate_email_domain_failure
from app.contacts.mail_routing import (
    DnsNameNotFound,
    DnsNoAnswer,
    DnsTemporaryError,
    MailRoutingChecker,
    MailRoutingStatus,
)
from app.email.providers import FakeGmailProvider
from app.email.service import EmailSendBlocked, EmailService
from app.models.entities import (
    Application,
    EmailDelivery,
    EmailSendAttempt,
    EmployerContact,
    SourceJob,
)
from app.models.enums import (
    ApplicationStatus,
    ContactDeliveryState,
    DeliveryStatus,
    MatchDecision,
    PolicyDecision,
)
from tests.unit.test_daily_minimum import additional_candidate
from tests.unit.test_policy_and_email import make_graph, settings


class FakeLookup:
    """Offline DNS: maps (name, rdtype) to records or an exception class."""

    def __init__(self, records: dict[tuple[str, str], list[str] | type[Exception]]) -> None:
        self.records = records
        self.calls: list[tuple[str, str]] = []

    async def query(self, name: str, rdtype: str) -> list[str]:
        self.calls.append((name, rdtype))
        value = self.records.get((name, rdtype), DnsNoAnswer)
        if isinstance(value, type) and issubclass(value, Exception):
            raise value(name)
        return list(value)


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        ({("example.com", "MX"): ["mx1.example.com."]}, MailRoutingStatus.ROUTABLE),
        ({("example.com", "MX"): DnsNameNotFound}, MailRoutingStatus.NO_DOMAIN),
        ({("example.com", "MX"): ["."]}, MailRoutingStatus.NULL_MX),
        ({("example.com", "A"): ["192.0.2.10"]}, MailRoutingStatus.ROUTABLE),
        ({("example.com", "AAAA"): ["2001:db8::1"]}, MailRoutingStatus.ROUTABLE),
        ({}, MailRoutingStatus.NO_MAIL_ROUTING),
        ({("example.com", "MX"): DnsTemporaryError}, MailRoutingStatus.TEMPORARY_FAILURE),
        ({("example.com", "A"): DnsTemporaryError}, MailRoutingStatus.TEMPORARY_FAILURE),
        ({("example.com", "A"): DnsNameNotFound}, MailRoutingStatus.NO_DOMAIN),
    ],
)
async def test_mail_routing_classification(records, expected) -> None:
    checker = MailRoutingChecker(FakeLookup(records))
    result = await checker.check_email("HR@Example.com")
    assert result.domain == "example.com"
    assert result.status is expected


async def test_mail_routing_cache_uses_shorter_ttl_for_temporary_failures() -> None:
    now = [0.0]
    lookup = FakeLookup(
        {("dead.test", "MX"): DnsNameNotFound, ("slow.test", "MX"): DnsTemporaryError}
    )
    checker = MailRoutingChecker(
        lookup,
        cache_ttl_seconds=3600,
        temporary_cache_ttl_seconds=60,
        clock=lambda: now[0],
    )
    await checker.check_domain("dead.test")
    await checker.check_domain("slow.test")
    now[0] = 120.0
    await checker.check_domain("dead.test")
    await checker.check_domain("slow.test")
    assert lookup.calls.count(("dead.test", "MX")) == 1
    assert lookup.calls.count(("slow.test", "MX")) == 2


async def _approved(session_factory, storage: Path, *, second_same_domain: bool = False):
    async with session_factory() as session:
        graph = await make_graph(session, storage)
        application = graph[8]
        application.status = ApplicationStatus.AUTO_APPROVED
        application.policy_decision = PolicyDecision.AUTO_APPROVED
        second_id = None
        if second_same_domain:
            second = await additional_candidate(
                session,
                graph,
                company="Second Company",
                score=92,
                decision=MatchDecision.AUTO_APPLY,
            )
            second_job: SourceJob = second[1]
            second_contact: EmployerContact = second[3]
            second_job.public_email = "hr@example.com"
            second_contact.value = "hr@example.com"
            second[4].status = ApplicationStatus.AUTO_APPROVED
            second[4].policy_decision = PolicyDecision.AUTO_APPROVED
            second_id = second[4].id
        await session.commit()
        return application.id, second_id


@pytest.mark.parametrize(
    "records",
    [
        {("example.com", "MX"): DnsNameNotFound},
        {("example.com", "MX"): ["."]},
        {},
    ],
    ids=["nxdomain", "null_mx", "no_routing"],
)
async def test_dead_domain_never_reaches_provider_and_blocks_same_domain_contacts(
    sqlite_session_factory, tmp_path: Path, records
) -> None:
    first_id, second_id = await _approved(sqlite_session_factory, tmp_path, second_same_domain=True)
    provider = FakeGmailProvider()
    service = EmailService(
        settings(tmp_path),
        sqlite_session_factory,
        provider,
        mail_routing=MailRoutingChecker(FakeLookup(records)),
    )

    with pytest.raises(EmailSendBlocked) as blocked:
        await service.send_application(first_id)
    assert blocked.value.reason == "recipient_domain_unroutable"
    assert provider.outbox == []

    async with sqlite_session_factory() as session:
        first = await session.get(Application, first_id)
        second = await session.get(Application, second_id)
        assert first is not None and second is not None
        assert first.status is ApplicationStatus.BLOCKED
        assert "contact_mail_routing" in first.policy_result["rules_failed"]
        contacts = (
            await session.scalars(
                select(EmployerContact).where(EmployerContact.value.like("%@example.com"))
            )
        ).all()
        assert {contact.value for contact in contacts} == {"jobs@example.com", "hr@example.com"}
        assert {contact.delivery_state for contact in contacts} == {ContactDeliveryState.REJECTED}
        assert await session.scalar(select(func.count(EmailDelivery.id))) == 0
        assert await session.scalar(select(func.count(EmailSendAttempt.id))) == 0

    # The second contact of the dead domain is blocked by the persisted state even
    # when no fresh lookup is available.
    no_dns_service = EmailService(settings(tmp_path), sqlite_session_factory, provider)
    with pytest.raises(EmailSendBlocked):
        await no_dns_service.send_application(second_id)
    assert provider.outbox == []
    async with sqlite_session_factory() as session:
        second = await session.get(Application, second_id)
        assert second is not None
        assert second.status is ApplicationStatus.BLOCKED
        assert "contact_delivery_usable" in second.policy_result["rules_failed"]


async def test_temporary_dns_failure_postpones_without_rejecting_contact(
    sqlite_session_factory, tmp_path: Path
) -> None:
    application_id, _ = await _approved(sqlite_session_factory, tmp_path)
    provider = FakeGmailProvider()
    service = EmailService(
        settings(tmp_path),
        sqlite_session_factory,
        provider,
        mail_routing=MailRoutingChecker(FakeLookup({("example.com", "MX"): DnsTemporaryError})),
    )

    with pytest.raises(EmailSendBlocked) as blocked:
        await service.send_application(application_id)
    assert blocked.value.reason == "mail_routing_temporarily_unavailable"
    assert provider.outbox == []
    async with sqlite_session_factory() as session:
        application = await session.get(Application, application_id)
        contact = await session.scalar(
            select(EmployerContact).where(EmployerContact.value == "jobs@example.com")
        )
        assert application is not None and contact is not None
        assert application.status is ApplicationStatus.AUTO_APPROVED
        assert contact.delivery_state is not ContactDeliveryState.REJECTED
        assert await session.scalar(select(func.count(EmailDelivery.id))) == 0


async def test_routable_domain_continues_to_policy_and_provider(
    sqlite_session_factory, tmp_path: Path
) -> None:
    application_id, _ = await _approved(sqlite_session_factory, tmp_path)
    provider = FakeGmailProvider()
    service = EmailService(
        settings(tmp_path),
        sqlite_session_factory,
        provider,
        mail_routing=MailRoutingChecker(FakeLookup({("example.com", "MX"): ["mx.example.com."]})),
    )
    delivery = await service.send_application(application_id)
    assert delivery.status is DeliveryStatus.PROVIDER_ACCEPTED
    assert len(provider.outbox) == 1


async def test_domain_propagation_skips_other_domains_and_suppressed_contacts(
    sqlite_session_factory, tmp_path: Path
) -> None:
    async with sqlite_session_factory() as session:
        graph = await make_graph(session, tmp_path)
        contact = graph[7]
        base = {
            column.key: getattr(contact, column.key)
            for column in EmployerContact.__table__.columns
            if column.key != "id"
        }
        other = EmployerContact(**{**base, "value": "jobs@example.com.evil.test"})
        suppressed = EmployerContact(
            **{
                **base,
                "value": "boss@example.com",
                "delivery_state": ContactDeliveryState.SUPPRESSED,
            }
        )
        session.add_all([other, suppressed])
        await session.flush()
        changed = await propagate_email_domain_failure(
            session,
            domain="Example.com.",
            occurred_at=datetime.now(UTC),
            failure_reason="domain_not_found",
        )
        assert changed == 1
        assert contact.delivery_state is ContactDeliveryState.REJECTED
        assert other.delivery_state is not ContactDeliveryState.REJECTED
        assert suppressed.delivery_state is ContactDeliveryState.SUPPRESSED
