from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Protocol
from uuid import UUID

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit import record_audit_event
from app.contacts import (
    failure_reason_after_pause,
    propagate_email_delivery_failure,
    propagate_email_domain_failure,
    release_paused_email_contacts,
    select_best_email_contact,
    validate_public_email,
)
from app.email.failures import mentions_tls_failure
from app.email.oauth import GmailOAuthService
from app.email.providers import (
    GMAIL_READONLY_SCOPE,
    GMAIL_REAUTH_REQUIRED_CODE,
    GmailReauthorizationRequired,
    TemporaryDeliveryError,
)
from app.email.retries import retry_delay
from app.employers import EmployerRelationshipService
from app.models.constants import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.models.entities import (
    Alert,
    Application,
    EmailDelivery,
    EmailDeliveryEvent,
    EmailMailboxCursor,
    EmployerContact,
    OAuthCredential,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    ContactDeliveryState,
    ContactType,
    DeliveryStatus,
    EmployerInteractionChannel,
    EmployerInteractionType,
)
from app.observability.metrics import EMAIL_DELIVERIES
from app.settings import Settings

_STATUS_RE = re.compile(r"(?<!\d)([245]\d\d)(?:[ -]([245]\.\d\.\d))?(?!\d)")
_ENHANCED_STATUS_RE = re.compile(r"(?<!\d)([245]\.\d\.\d)(?!\d)")
_MESSAGE_ID_RE = re.compile(r"<[^<>\s]+@[^<>\s]+>")
_DSN_SUBJECT_MARKERS = (
    "delivery status notification",
    "undelivered mail returned",
    "mail delivery failed",
    "delivery failure",
    "message blocked",
    "address not found",
    "undeliverable",
)


@dataclass(frozen=True)
class BounceClassification:
    status: DeliveryStatus
    failure_class: str
    permanent: bool
    retryable: bool
    smtp_status: str | None
    reason: str


@dataclass(frozen=True)
class ParsedDeliveryNotice:
    is_dsn: bool
    structured: bool
    original_message_id: str | None
    final_recipient: str | None
    diagnostic: str
    status: str | None
    action: str | None
    reporting_mta: str | None
    remote_mta: str | None
    occurred_at: datetime
    subject: str
    automated_sender: bool


@dataclass(frozen=True)
class MailboxMessage:
    message_id: str
    thread_id: str | None
    history_id: str | None
    raw: bytes
    inbox: bool = False


@dataclass(frozen=True)
class MailboxBatch:
    messages: tuple[MailboxMessage, ...]
    next_history_id: str | None


class MailboxProvider(Protocol):
    async def fetch(
        self, *, start_history_id: str | None, max_results: int, monitor_days: int
    ) -> MailboxBatch: ...


def _normalized_recipient(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.split(";", maxsplit=1)[-1].strip()
    addresses = [address.casefold() for _name, address in getaddresses([candidate]) if address]
    return addresses[0] if addresses else (candidate.casefold() if "@" in candidate else None)


def classify_smtp_failure(
    diagnostic: str,
    *,
    status: str | None = None,
    action: str | None = None,
) -> BounceClassification:
    text = " ".join((status or "", diagnostic)).strip()
    folded = text.casefold()
    match = _STATUS_RE.search(status or "") or _STATUS_RE.search(diagnostic)
    enhanced = _ENHANCED_STATUS_RE.search(status or "") or _ENHANCED_STATUS_RE.search(diagnostic)
    basic_code = match.group(1) if match else None
    enhanced_code = enhanced.group(1) if enhanced else None
    # A diagnostic can contain an unrelated 3-digit number. A structured DSN
    # Status or enhanced status code takes precedence over a conflicting number.
    if basic_code and enhanced_code and basic_code[0] != enhanced_code[0]:
        basic_code = None
    smtp_status = " ".join(value for value in (basic_code, enhanced_code) if value) or None
    leading = enhanced_code[0] if enhanced_code else (basic_code[0] if basic_code else "")
    normalized_action = (action or "").strip().casefold()
    # "failed" with a 4.x.x status: the provider retried for days and gave up.
    # Sending again only restarts the same doomed retry window.
    expired = normalized_action == "failed" and leading == "4"
    permanent = leading == "5" or expired
    retryable = leading == "4" and not expired

    if normalized_action in {"delayed", "relayed", "expanded"}:
        return BounceClassification(
            DeliveryStatus.PROVIDER_ACCEPTED,
            f"delivery_{normalized_action}",
            False,
            False,
            smtp_status,
            text[:500] or normalized_action,
        )
    if (normalized_action == "delivered" and leading != "2") or (
        normalized_action == "failed" and leading == "2"
    ):
        return BounceClassification(
            DeliveryStatus.PROVIDER_ACCEPTED,
            "inconsistent_delivery_notice",
            False,
            False,
            smtp_status,
            text[:500] or "inconsistent_delivery_notice",
        )
    if leading == "2":
        failure_class = "accepted"
        delivery_status = (
            DeliveryStatus.DELIVERED
            if normalized_action == "delivered"
            else DeliveryStatus.PROVIDER_ACCEPTED
        )
        permanent = False
        retryable = False
    elif mentions_tls_failure(folded):
        failure_class = "tls_failure"
        delivery_status = (
            DeliveryStatus.BOUNCED_TRANSIENT if retryable else DeliveryStatus.DOMAIN_REJECTED
        )
    elif any(marker in folded for marker in ("mailbox full", "quota exceeded", "5.2.2", "4.2.2")):
        failure_class = "mailbox_full"
        delivery_status = (
            DeliveryStatus.BOUNCED_PERMANENT if permanent else DeliveryStatus.MAILBOX_FULL
        )
    elif any(
        marker in folded
        for marker in (
            "recipient address rejected",
            "access denied",
            "recipient rejected",
            "5.4.1",
        )
    ):
        failure_class = "recipient_rejected"
        delivery_status = (
            DeliveryStatus.BOUNCED_TRANSIENT if retryable else DeliveryStatus.RECIPIENT_REJECTED
        )
        permanent = True if leading != "4" else permanent
        retryable = False if permanent else retryable
    elif any(
        marker in folded
        for marker in (
            "user unknown",
            "address not found",
            "no such user",
            "no such person",
            "no such mailbox",
            "recipient does not exist",
            "unknown recipient",
            "recipient not found",
            "invalid mailbox",
            "account is disabled",
            "mailbox is unavailable",
            "5.1.1",
        )
    ):
        failure_class = "recipient_not_found"
        delivery_status = (
            DeliveryStatus.BOUNCED_TRANSIENT if retryable else DeliveryStatus.RECIPIENT_REJECTED
        )
        permanent = not retryable
    elif any(marker in folded for marker in ("spam", "5.7.1", "unsolicited", "blacklist")):
        failure_class = "spam_policy"
        delivery_status = (
            DeliveryStatus.BOUNCED_TRANSIENT if retryable else DeliveryStatus.SPAM_REJECTED
        )
    elif any(
        marker in folded
        for marker in (
            "domain not found",
            "domain name not found",
            "no such domain",
            "host not found",
            "name service error",
            "dns",
            "5.1.2",
        )
    ):
        failure_class = "domain_not_found"
        delivery_status = (
            DeliveryStatus.BOUNCED_TRANSIENT if retryable else DeliveryStatus.DOMAIN_REJECTED
        )
    elif any(marker in folded for marker in ("message too large", "size limit", "5.3.4")):
        failure_class = "message_too_large"
        delivery_status = DeliveryStatus.BOUNCED_PERMANENT
        permanent = True
        retryable = False
    elif any(marker in folded for marker in ("routing", "no route", "5.4.4")):
        failure_class = "routing_failure"
        delivery_status = (
            DeliveryStatus.BOUNCED_TRANSIENT if retryable else DeliveryStatus.DOMAIN_REJECTED
        )
    elif any(marker in folded for marker in ("policy rejected", "policy violation", "blocked")):
        failure_class = "policy_rejected"
        delivery_status = (
            DeliveryStatus.BOUNCED_TRANSIENT if retryable else DeliveryStatus.POLICY_REJECTED
        )
    elif any(
        marker in folded
        for marker in (
            "authentication required",
            "authentication failed",
            "not authenticated",
            "sender not authenticated",
            "spf failed",
            "spf failure",
            "dkim failed",
            "dkim failure",
            "dmarc failed",
            "dmarc failure",
        )
    ):
        failure_class = "authentication_failure"
        delivery_status = (
            DeliveryStatus.BOUNCED_TRANSIENT if retryable else DeliveryStatus.POLICY_REJECTED
        )
    elif expired:
        failure_class = "delivery_expired"
        delivery_status = DeliveryStatus.BOUNCED_PERMANENT
    elif any(marker in folded for marker in ("rate limit", "too many", "4.7.")):
        failure_class = "rate_limited"
        delivery_status = DeliveryStatus.BOUNCED_TRANSIENT
        retryable = True
        permanent = False
    elif leading == "4":
        failure_class = "remote_server_unavailable"
        delivery_status = DeliveryStatus.BOUNCED_TRANSIENT
    elif leading == "5":
        failure_class = "unknown_bounce"
        delivery_status = DeliveryStatus.BOUNCED_PERMANENT
    else:
        failure_class = "unknown_bounce"
        delivery_status = DeliveryStatus.DELIVERY_FAILED
    reason = text[:500] or failure_class
    return BounceClassification(
        delivery_status,
        failure_class,
        permanent,
        retryable and not permanent,
        smtp_status,
        reason,
    )


def _message_text(message: Message) -> str:
    values: list[str] = []
    for part in message.walk():
        if part.get_content_type() != "text/plain":
            continue
        try:
            payload = part.get_payload(decode=True)
        except (LookupError, UnicodeError):
            continue
        if isinstance(payload, bytes):
            values.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        else:
            raw_payload = part.get_payload()
            if isinstance(raw_payload, str):
                values.append(raw_payload)
    return "\n".join(values)[:100_000]


def parse_delivery_notice(
    raw: bytes, *, received_at: datetime | None = None
) -> ParsedDeliveryNotice:
    message = message_from_bytes(raw, policy=policy.default)
    subject = str(message.get("Subject", ""))
    sender = str(message.get("From", ""))
    diagnostic_values: list[str] = []
    original_message_id: str | None = None
    final_recipient: str | None = None
    status: str | None = None
    action: str | None = None
    reporting_mta: str | None = None
    remote_mta: str | None = None
    structured = False
    for part in message.walk():
        if part.get_content_type() == "message/delivery-status":
            structured = True
            payload = part.get_payload()
            blocks = payload if isinstance(payload, list) else []
            for block in blocks:
                if not isinstance(block, Message):
                    continue
                diagnostic_values.extend(block.get_all("Diagnostic-Code", []))
                status = status or block.get("Status")
                action = action or block.get("Action")
                reporting_mta = reporting_mta or block.get("Reporting-MTA")
                remote_mta = remote_mta or block.get("Remote-MTA")
                final_recipient = final_recipient or _normalized_recipient(
                    block.get("Final-Recipient") or block.get("Original-Recipient")
                )
                original_message_id = original_message_id or block.get("Original-Message-ID")
        elif part.get_content_type() in {"message/rfc822", "text/rfc822-headers"}:
            payload = part.get_payload()
            original = payload[0] if isinstance(payload, list) and payload else None
            if isinstance(original, Message):
                original_message_id = original_message_id or original.get("Message-ID")
                final_recipient = final_recipient or _normalized_recipient(original.get("To"))
    # Structured DSN fields are authoritative. Prose in the human-readable
    # part can mention a different recipient or message and must not supply
    # correlation identifiers when the delivery-status part omits them.
    fallback = "" if structured else _message_text(message)
    if not original_message_id:
        match = _MESSAGE_ID_RE.search(fallback)
        original_message_id = match.group(0) if match else None
    if not final_recipient:
        recipient_match = re.search(
            r"(?im)^(?:final|original)-recipient:\s*(?:rfc822;)?\s*([^\s<>]+@[^\s<>]+)",
            fallback,
        )
        final_recipient = (
            _normalized_recipient(recipient_match.group(1)) if recipient_match else None
        )
    diagnostic = "\n".join(diagnostic_values) or fallback
    folded_subject = subject.casefold()
    sender_addresses = {address.casefold() for _name, address in getaddresses([sender])}
    system_sender = any(
        address.split("@", maxsplit=1)[0] in {"mailer-daemon", "postmaster"}
        for address in sender_addresses
        if "@" in address
    )
    has_failure_code = bool(_STATUS_RE.search(" ".join((status or "", diagnostic))))
    is_dsn = structured or (
        (system_sender or any(marker in folded_subject for marker in _DSN_SUBJECT_MARKERS))
        and has_failure_code
    )
    occurred_at = received_at or datetime.now(UTC)
    date_header = message.get("Date")
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            occurred_at = parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError, OverflowError):
            pass
    return ParsedDeliveryNotice(
        is_dsn=is_dsn,
        structured=structured,
        original_message_id=original_message_id,
        final_recipient=final_recipient,
        diagnostic=diagnostic,
        status=status,
        action=action,
        reporting_mta=reporting_mta,
        remote_mta=remote_mta,
        occurred_at=occurred_at,
        subject=subject,
        automated_sender=system_sender,
    )


class GmailMailboxProvider:
    def __init__(self, *, client_id: str, client_secret: str, refresh_token: str) -> None:
        self._credentials = Credentials(  # type: ignore[no-untyped-call]
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
            client_id=client_id,
            client_secret=client_secret,
            scopes=[GMAIL_READONLY_SCOPE],
        )

    async def fetch(
        self, *, start_history_id: str | None, max_results: int, monitor_days: int
    ) -> MailboxBatch:
        def execute() -> MailboxBatch:
            service = build("gmail", "v1", credentials=self._credentials, cache_discovery=False)
            profile_result = service.users().getProfile(userId="me").execute()
            profile_history_id = str(profile_result.get("historyId") or "") or None
            candidates: list[tuple[str, str | None, str | None]] = []
            next_history_id = start_history_id or profile_history_id
            if start_history_id:
                page_token: str | None = None
                stop = False
                while not stop:
                    response = (
                        service.users()
                        .history()
                        .list(
                            userId="me",
                            startHistoryId=start_history_id,
                            historyTypes=["messageAdded"],
                            pageToken=page_token,
                            maxResults=min(max_results, 500),
                        )
                        .execute()
                    )
                    for record in response.get("history", []):
                        history_id = str(record.get("id") or "") or None
                        for added in record.get("messagesAdded", []):
                            item = added.get("message", {})
                            message_id = item.get("id")
                            if isinstance(message_id, str):
                                candidates.append((message_id, item.get("threadId"), history_id))
                        # Advance only after the entire history record has been
                        # collected. Several messages can share one history ID.
                        next_history_id = history_id or next_history_id
                        if len(candidates) >= max_results:
                            stop = True
                            break
                    page_token = response.get("nextPageToken")
                    if not page_token:
                        break
                if not stop:
                    next_history_id = profile_history_id or next_history_id
            else:
                queries = (
                    f"newer_than:{monitor_days}d "
                    "{from:mailer-daemon from:postmaster subject:(delivery failure) "
                    "subject:(delivery status notification) subject:(undeliverable)}",
                    f"newer_than:{monitor_days}d in:inbox",
                )
                seen: set[str] = set()
                for query in queries:
                    page_token = None
                    while True:
                        response = (
                            service.users()
                            .messages()
                            .list(
                                userId="me",
                                q=query,
                                maxResults=min(max_results, 500),
                                pageToken=page_token,
                            )
                            .execute()
                        )
                        for item in response.get("messages", []):
                            message_id = item.get("id")
                            if isinstance(message_id, str) and message_id not in seen:
                                seen.add(message_id)
                                candidates.append((message_id, item.get("threadId"), None))
                        page_token = response.get("nextPageToken")
                        if not page_token:
                            break
            messages: list[MailboxMessage] = []
            for message_id, thread_id, history_id in candidates:
                response = (
                    service.users()
                    .messages()
                    .get(userId="me", id=message_id, format="raw")
                    .execute()
                )
                raw_value = response.get("raw")
                if not isinstance(raw_value, str):
                    continue
                padded = raw_value + "=" * (-len(raw_value) % 4)
                messages.append(
                    MailboxMessage(
                        message_id=message_id,
                        thread_id=(
                            response.get("threadId")
                            if isinstance(response.get("threadId"), str)
                            else thread_id
                        ),
                        history_id=history_id,
                        raw=base64.urlsafe_b64decode(padded.encode("ascii")),
                        inbox="INBOX" in response.get("labelIds", []),
                    )
                )
            return MailboxBatch(tuple(messages), next_history_id)

        try:
            return await asyncio.to_thread(execute)
        except HttpError as exc:
            if start_history_id and getattr(exc.resp, "status", None) == 404:
                return await self.fetch(
                    start_history_id=None,
                    max_results=max_results,
                    monitor_days=monitor_days,
                )
            raise RuntimeError("Gmail delivery reconciliation failed") from exc
        except RefreshError as exc:
            if exc.retryable:
                raise TemporaryDeliveryError(
                    "Gmail token refresh is temporarily unavailable"
                ) from exc
            raise GmailReauthorizationRequired("Gmail reauthorization is required") from exc


# Failures of the recipient domain as a whole, not of one mailbox.
DOMAIN_FAILURE_CLASSES = frozenset({"domain_not_found", "routing_failure", "tls_failure"})
# Delivery states that a later notice may still turn into a final outcome.
_AWAITING_OUTCOME = frozenset(
    {DeliveryStatus.SUBMITTED, DeliveryStatus.PROVIDER_ACCEPTED, DeliveryStatus.SENT}
)


def _email_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().rstrip(".").casefold()


def delay_alert_code(delivery_id: UUID) -> str:
    return f"email_delivery_delayed:{delivery_id}"


async def _resolve_delay_alert(
    session: AsyncSession, delivery: EmailDelivery, outcome: str, occurred_at: datetime
) -> None:
    """The final outcome answers the delay warning; it no longer needs attention."""

    if delivery.failure_class != "delivery_delayed":
        return
    alert = await session.scalar(select(Alert).where(Alert.code == delay_alert_code(delivery.id)))
    if alert is None or alert.acknowledged:
        return
    alert.acknowledged = True
    alert.safe_diagnostics = {
        **(alert.safe_diagnostics or {}),
        "resolution": {"reason": f"delivery_{outcome}", "at": occurred_at.isoformat()},
    }


class EmailDeliveryReconciliationService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        mailbox: MailboxProvider | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._mailbox = mailbox

    async def _mailbox_for(
        self,
        session: AsyncSession,
        *,
        account_id: UUID,
    ) -> MailboxProvider | None:
        if self._mailbox is not None:
            return self._mailbox
        credential = await session.scalar(
            select(OAuthCredential).where(
                OAuthCredential.account_id == account_id,
                OAuthCredential.provider == "gmail",
            )
        )
        if credential is None or GMAIL_READONLY_SCOPE not in credential.scopes:
            return None
        if self.settings.gmail_client_id is None or self.settings.gmail_client_secret is None:
            return None
        token = await GmailOAuthService(self.settings).get_refresh_token(
            session,
            account_id=account_id,
            required_scopes=(GMAIL_READONLY_SCOPE,),
        )
        return GmailMailboxProvider(
            client_id=self.settings.gmail_client_id.get_secret_value(),
            client_secret=self.settings.gmail_client_secret.get_secret_value(),
            refresh_token=token,
        )

    @staticmethod
    async def _correlate(
        session: AsyncSession,
        notice: ParsedDeliveryNotice,
        *,
        account_id: UUID,
        thread_id: str | None,
    ) -> EmailDelivery | None:
        if notice.original_message_id:
            delivery = await session.scalar(
                select(EmailDelivery)
                .join(Application, Application.id == EmailDelivery.application_id)
                .join(UserProfile, UserProfile.id == Application.profile_id)
                .where(
                    UserProfile.owner_account_id == account_id,
                    EmailDelivery.rfc_message_id == notice.original_message_id,
                    *(
                        [EmailDelivery.recipient == notice.final_recipient]
                        if notice.final_recipient
                        else []
                    ),
                )
            )
            if delivery is not None:
                return delivery
        # A DSN needs its recipient to disambiguate an outbound Gmail thread.
        # A human reply is linked by thread even though it has no DSN recipient.
        if thread_id and (notice.final_recipient or not notice.is_dsn):
            rows = list(
                (
                    await session.scalars(
                        select(EmailDelivery)
                        .join(Application, Application.id == EmailDelivery.application_id)
                        .join(UserProfile, UserProfile.id == Application.profile_id)
                        .where(
                            UserProfile.owner_account_id == account_id,
                            EmailDelivery.thread_id == thread_id,
                            *(
                                [EmailDelivery.recipient == notice.final_recipient]
                                if notice.final_recipient
                                else []
                            ),
                        )
                    )
                ).all()
            )
            if len(rows) == 1:
                return rows[0]
        if notice.final_recipient:
            lower_bound = notice.occurred_at - timedelta(days=30)
            rows = list(
                (
                    await session.scalars(
                        select(EmailDelivery)
                        .join(Application, Application.id == EmailDelivery.application_id)
                        .join(UserProfile, UserProfile.id == Application.profile_id)
                        .where(
                            UserProfile.owner_account_id == account_id,
                            EmailDelivery.recipient == notice.final_recipient,
                            EmailDelivery.created_at >= lower_bound,
                            EmailDelivery.created_at <= notice.occurred_at,
                        )
                        .order_by(EmailDelivery.created_at.desc())
                        .limit(2)
                    )
                ).all()
            )
            if len(rows) == 1:
                return rows[0]
        return None

    async def _apply_notice(
        self,
        session: AsyncSession,
        mailbox_message: MailboxMessage,
        notice: ParsedDeliveryNotice,
        *,
        account_id: UUID,
    ) -> tuple[bool, bool]:
        existing = await session.scalar(
            select(EmailDeliveryEvent.id).where(
                EmailDeliveryEvent.account_id == account_id,
                EmailDeliveryEvent.provider == "gmail",
                EmailDeliveryEvent.provider_message_id == mailbox_message.message_id,
            )
        )
        if existing is not None:
            return False, False
        delivery = (
            await self._correlate(
                session,
                notice,
                account_id=account_id,
                thread_id=mailbox_message.thread_id,
            )
            if notice.is_dsn or (mailbox_message.inbox and not notice.automated_sender)
            else None
        )
        classification = (
            classify_smtp_failure(notice.diagnostic, status=notice.status, action=notice.action)
            if notice.is_dsn
            else None
        )
        event = EmailDeliveryEvent(
            account_id=account_id,
            delivery_id=delivery.id if delivery is not None else None,
            provider="gmail",
            provider_message_id=mailbox_message.message_id,
            provider_thread_id=mailbox_message.thread_id,
            event_type=(
                "delivery_confirmation"
                if classification is not None and classification.status is DeliveryStatus.DELIVERED
                else "delivery_notice"
                if classification is not None
                and classification.status is DeliveryStatus.PROVIDER_ACCEPTED
                else "bounce"
                if notice.is_dsn
                else "employer_reply"
                if delivery is not None and mailbox_message.inbox
                else "ignored_message"
            ),
            original_message_id=notice.original_message_id,
            final_recipient=notice.final_recipient,
            smtp_status=classification.smtp_status if classification else None,
            failure_class=classification.failure_class if classification else None,
            failure_reason=classification.reason if classification else None,
            permanent=classification.permanent if classification else False,
            retryable=classification.retryable if classification else False,
            occurred_at=notice.occurred_at,
            safe_metadata={
                "structured": notice.structured,
                "action": notice.action,
                "reporting_mta": notice.reporting_mta,
                "remote_mta": notice.remote_mta,
                "subject_fingerprint": hashlib.sha256(
                    notice.subject.encode("utf-8", errors="replace")
                ).hexdigest(),
            },
        )
        session.add(event)
        if (
            not notice.is_dsn
            and mailbox_message.inbox
            and not notice.automated_sender
            and delivery is not None
        ):
            application = await session.get(Application, delivery.application_id)
            if application is not None and application.employer_id is not None:
                await EmployerRelationshipService().record_event(
                    session,
                    profile_id=application.profile_id,
                    employer_id=application.employer_id,
                    event_type=EmployerInteractionType.EMPLOYER_REPLIED,
                    channel=EmployerInteractionChannel.EMAIL,
                    idempotency_key=(f"gmail-reply:{account_id}:{mailbox_message.message_id}"),
                    occurred_at=notice.occurred_at,
                    application_id=application.id,
                    canonical_job_id=application.canonical_job_id,
                    source_job_id=application.source_job_id,
                    event_metadata={
                        "provider_message_id": mailbox_message.message_id,
                        "thread_id": mailbox_message.thread_id,
                    },
                )
            await session.flush()
            return True, application is not None
        if delivery is None or classification is None:
            await session.flush()
            return True, False

        if classification.status is DeliveryStatus.PROVIDER_ACCEPTED:
            # A delayed/relayed notice or bare 2xx acknowledgment is not proof
            # of final delivery and must not schedule a duplicate send.
            if (
                classification.failure_class == "delivery_delayed"
                and delivery.status in _AWAITING_OUTCOME
            ):
                await self._record_delay(session, delivery, classification, notice)
            await session.flush()
            return True, True

        terminal_statuses = {
            DeliveryStatus.BOUNCED_PERMANENT,
            DeliveryStatus.RECIPIENT_REJECTED,
            DeliveryStatus.DOMAIN_REJECTED,
            DeliveryStatus.POLICY_REJECTED,
            DeliveryStatus.SPAM_REJECTED,
            DeliveryStatus.PERMANENT_FAILURE,
        }
        if delivery.status in terminal_statuses or (
            delivery.status is DeliveryStatus.DELIVERED and classification.retryable
        ):
            event.safe_metadata = {
                **event.safe_metadata,
                "ignored_reason": "terminal_delivery_outcome_preserved",
            }
            await session.flush()
            return True, True

        if classification.status is DeliveryStatus.DELIVERED:
            await _resolve_delay_alert(session, delivery, "delivered", notice.occurred_at)
            delivery.status = DeliveryStatus.DELIVERED
            delivery.final_recipient = notice.final_recipient or delivery.recipient
            delivery.smtp_status = classification.smtp_status
            delivery.error = None
            delivery.error_code = None
            delivery.failure_class = None
            delivery.failure_reason = None
            application = await session.get(Application, delivery.application_id)
            contact = (
                await session.get(EmployerContact, application.recipient_contact_id)
                if application is not None
                else None
            )
            if contact is not None and contact.delivery_state not in {
                ContactDeliveryState.INVALID,
                ContactDeliveryState.REJECTED,
                ContactDeliveryState.SUPPRESSED,
            }:
                contact.delivery_state = ContactDeliveryState.HEALTHY
            await record_audit_event(
                session,
                actor="email_reconciler",
                action="email.delivery_confirmed",
                entity_type="email_delivery",
                entity_id=str(delivery.id),
                correlation_id=str(delivery.application_id),
                decision=DeliveryStatus.DELIVERED.value,
                details={"smtp_status": classification.smtp_status},
            )
            EMAIL_DELIVERIES.labels(
                provider=delivery.provider,
                state=DeliveryStatus.DELIVERED.value,
            ).inc()
            await session.flush()
            return True, True

        await _resolve_delay_alert(
            session, delivery, classification.status.value, notice.occurred_at
        )
        delivery.status = classification.status
        delivery.smtp_status = classification.smtp_status
        delivery.failure_class = classification.failure_class
        delivery.failure_reason = classification.reason
        delivery.bounced_at = notice.occurred_at
        delivery.final_recipient = notice.final_recipient or delivery.recipient
        delivery.error = classification.reason
        delivery.error_code = classification.failure_class
        application = await session.get(Application, delivery.application_id)
        contact = (
            await session.get(EmployerContact, application.recipient_contact_id)
            if application is not None
            else None
        )
        if application is not None:
            # SENT records preserve sent_at, but the current application outcome
            # must reflect that the provider did not deliver the message.
            application.status = ApplicationStatus.FAILED

        if classification.retryable:
            if delivery.attempt_count < self.settings.email_delivery_max_attempts:
                delivery.next_retry_at = notice.occurred_at + retry_delay(delivery.attempt_count)
            else:
                delivery.next_retry_at = None
            if contact is not None:
                contact.delivery_state = ContactDeliveryState.TRANSIENT_FAILURE
        else:
            delivery.next_retry_at = None
            if contact is not None:
                if classification.permanent:
                    contact.delivery_state = (
                        ContactDeliveryState.INVALID
                        if classification.failure_class == "recipient_not_found"
                        else ContactDeliveryState.REJECTED
                    )
                else:
                    contact.delivery_state = ContactDeliveryState.TRANSIENT_FAILURE
            if (
                classification.permanent
                and application is not None
                and application.employer_id is not None
            ):
                await EmployerRelationshipService().record_event(
                    session,
                    profile_id=application.profile_id,
                    employer_id=application.employer_id,
                    event_type=EmployerInteractionType.APPLICATION_DELIVERY_FAILED,
                    channel=EmployerInteractionChannel.EMAIL,
                    idempotency_key=(
                        f"delivery-failed:{account_id}:{event.provider}:{event.provider_message_id}"
                    ),
                    occurred_at=notice.occurred_at,
                    application_id=application.id,
                    canonical_job_id=application.canonical_job_id,
                    source_job_id=application.source_job_id,
                    event_metadata={
                        "delivery_id": str(delivery.id),
                        "failure_class": classification.failure_class,
                    },
                )

                alternate = None
                alternate_recipient: str | None = None
                if contact is not None:
                    alternate_query = select(EmployerContact).where(
                        EmployerContact.source_job_id == application.source_job_id,
                        EmployerContact.contact_type == ContactType.EMAIL,
                        EmployerContact.id != contact.id,
                    )
                    if application.employer_id is not None:
                        alternate_query = alternate_query.where(
                            EmployerContact.employer_id == application.employer_id
                        )
                    alternate_contacts = list((await session.scalars(alternate_query)).all())
                    source_job = await session.get(SourceJob, application.source_job_id)
                    current_public_emails = (
                        {
                            normalized
                            for value in [
                                source_job.public_email,
                                *(source_job.public_emails or []),
                            ]
                            if value and (normalized := validate_public_email(value))
                        }
                        if source_job is not None
                        else set()
                    )
                    failed_domain = _email_domain(delivery.final_recipient or delivery.recipient)
                    alternate_contacts = [
                        candidate
                        for candidate in alternate_contacts
                        if candidate.value in current_public_emails
                        and not (
                            classification.failure_class in DOMAIN_FAILURE_CLASSES
                            and _email_domain(candidate.value) == failed_domain
                        )
                    ]
                    alternate = select_best_email_contact(alternate_contacts)
                    alternate_recipient = alternate.value if alternate is not None else None

                fallback_metadata = dict(delivery.sanitized_provider_response)
                fallback_already_used = bool(
                    fallback_metadata.get("recipient_fallback_used")
                    or fallback_metadata.get("recipient_fallback_pending")
                )
                fallback_allowed = (
                    self.settings.email_recipient_fallback_enabled
                    and classification.failure_class
                    in {"recipient_not_found", "recipient_rejected"}
                    and alternate is not None
                    and not fallback_already_used
                    and delivery.attempt_count < self.settings.email_delivery_max_attempts
                )
                if fallback_allowed and alternate is not None:
                    previous_recipient = delivery.final_recipient or delivery.recipient
                    application.recipient_contact_id = alternate.id
                    delivery.next_retry_at = notice.occurred_at + retry_delay(
                        delivery.attempt_count
                    )
                    delivery.sanitized_provider_response = {
                        **fallback_metadata,
                        "recipient_fallback_pending": True,
                        "recipient_fallback_used": False,
                        "recipient_fallback_from": previous_recipient,
                        "recipient_fallback_to": alternate.value,
                    }
                    await record_audit_event(
                        session,
                        actor="email_reconciler",
                        action="email.recipient_fallback_scheduled",
                        entity_type="email_delivery",
                        entity_id=str(delivery.id),
                        correlation_id=str(application.id),
                        decision="scheduled",
                        details={
                            "from": previous_recipient,
                            "to": alternate.value,
                            "next_retry_at": delivery.next_retry_at.isoformat(),
                        },
                    )

                alert_code = f"email_permanent_delivery_failure:{delivery.id}"
                existing_alert = await session.scalar(
                    select(Alert.id).where(Alert.code == alert_code)
                )
                if existing_alert is None:
                    systemic_failure = classification.failure_class in {
                        "authentication_failure",
                        "spam_policy",
                        "policy_rejected",
                    }
                    session.add(
                        Alert(
                            severity="high" if systemic_failure else "warning",
                            code=alert_code,
                            message="Permanent email delivery failure",
                            safe_diagnostics={
                                "application_id": str(application.id),
                                "delivery_id": str(delivery.id),
                                "recipient": delivery.final_recipient or delivery.recipient,
                                "smtp_status": classification.smtp_status,
                                "failure_class": classification.failure_class,
                                "alternate_recipient": alternate_recipient,
                                "fallback_scheduled": fallback_allowed,
                            },
                            acknowledged=False,
                        )
                    )
        if contact is not None:
            # A final failure right after a pause ended is not fixed by waiting.
            failure_reason = (
                await failure_reason_after_pause(
                    session, contact=contact, failure_class=classification.failure_class
                )
                if classification.permanent
                else classification.failure_class
            )
            contact.last_delivery_failure_at = notice.occurred_at
            contact.last_smtp_status = classification.smtp_status
            contact.last_failure_reason = failure_reason
            contact.failure_count += 1
            if classification.permanent and classification.failure_class in {
                "recipient_not_found",
                "recipient_rejected",
            }:
                await propagate_email_delivery_failure(
                    session,
                    email=delivery.final_recipient or delivery.recipient,
                    employer_id=application.employer_id if application is not None else None,
                    state=(
                        ContactDeliveryState.INVALID
                        if classification.failure_class == "recipient_not_found"
                        else ContactDeliveryState.REJECTED
                    ),
                    occurred_at=notice.occurred_at,
                    smtp_status=classification.smtp_status,
                    failure_reason=classification.failure_class,
                )
            elif (
                classification.permanent and classification.failure_class in DOMAIN_FAILURE_CLASSES
            ):
                # A dead domain, or one whose server cannot do TLS, rejects every
                # mailbox: do not try its other contacts.
                await propagate_email_domain_failure(
                    session,
                    domain=_email_domain(delivery.final_recipient or delivery.recipient),
                    occurred_at=notice.occurred_at,
                    failure_reason=failure_reason,
                    smtp_status=classification.smtp_status,
                )
        await record_audit_event(
            session,
            actor="email_reconciler",
            action="email.delivery_bounced",
            entity_type="email_delivery",
            entity_id=str(delivery.id),
            correlation_id=str(delivery.application_id),
            decision=classification.status.value,
            details={
                "smtp_status": classification.smtp_status,
                "failure_class": classification.failure_class,
                "permanent": classification.permanent,
                "retryable": classification.retryable,
                "contact_id": str(contact.id) if contact is not None else None,
            },
        )
        EMAIL_DELIVERIES.labels(
            provider=delivery.provider,
            state=classification.status.value,
        ).inc()
        await session.flush()
        return True, True

    async def _record_delay(
        self,
        session: AsyncSession,
        delivery: EmailDelivery,
        classification: BounceClassification,
        notice: ParsedDeliveryNotice,
    ) -> None:
        """Keep the delivery accepted but make the provider's warning visible.

        The provider keeps retrying on its own; JobHunter must not send again.
        The final notice replaces these fields and answers the alert.
        """

        delivery.smtp_status = classification.smtp_status
        delivery.failure_class = classification.failure_class
        delivery.failure_reason = classification.reason
        alert_code = delay_alert_code(delivery.id)
        if await session.scalar(select(Alert.id).where(Alert.code == alert_code)) is not None:
            return
        session.add(
            Alert(
                severity="warning",
                code=alert_code,
                message="Email delivery delayed",
                safe_diagnostics={
                    "application_id": str(delivery.application_id),
                    "delivery_id": str(delivery.id),
                    "recipient": delivery.final_recipient or delivery.recipient,
                    "smtp_status": classification.smtp_status,
                    "reason": classification.reason,
                    "remote_mta": notice.remote_mta,
                },
                acknowledged=False,
            )
        )
        await record_audit_event(
            session,
            actor="email_reconciler",
            action="email.delivery_delayed",
            entity_type="email_delivery",
            entity_id=str(delivery.id),
            correlation_id=str(delivery.application_id),
            decision=delivery.status.value,
            details={"smtp_status": classification.smtp_status},
        )

    async def reconcile(
        self,
        *,
        account_id: UUID = BOOTSTRAP_ADMIN_ACCOUNT_ID,
    ) -> dict[str, int | str]:
        if not self.settings.gmail_delivery_reconciliation_enabled:
            return {"status": "disabled", "fetched": 0, "processed": 0, "correlated": 0}
        async with self.session_factory() as session:
            mailbox = await self._mailbox_for(session, account_id=account_id)
            if mailbox is None:
                return {
                    "status": "gmail_readonly_scope_required",
                    "fetched": 0,
                    "processed": 0,
                    "correlated": 0,
                }
            cursor = await session.get(EmailMailboxCursor, (account_id, "gmail"))
            start_history_id = cursor.history_id if cursor is not None else None
        try:
            batch = await mailbox.fetch(
                start_history_id=start_history_id,
                max_results=self.settings.gmail_delivery_reconciliation_batch,
                monitor_days=self.settings.gmail_delivery_monitor_days,
            )
        except GmailReauthorizationRequired:
            async with self.session_factory() as session:
                await GmailOAuthService(self.settings).mark_reauthorization_required(
                    session,
                    account_id=account_id,
                )
                await session.commit()
            return {
                "status": GMAIL_REAUTH_REQUIRED_CODE,
                "fetched": 0,
                "processed": 0,
                "correlated": 0,
            }
        except TemporaryDeliveryError:
            return {
                "status": "gmail_refresh_temporary_failure",
                "fetched": 0,
                "processed": 0,
                "correlated": 0,
            }
        processed = 0
        correlated = 0
        async with self.session_factory() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                    {"lock_key": f"jobhunter:gmail-delivery-reconciliation:{account_id}"},
                )
            cursor_query = select(EmailMailboxCursor).where(
                EmailMailboxCursor.account_id == account_id,
                EmailMailboxCursor.provider == "gmail",
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                cursor_query = cursor_query.with_for_update()
            cursor = await session.scalar(cursor_query)
            if cursor is None:
                cursor = EmailMailboxCursor(account_id=account_id, provider="gmail")
                session.add(cursor)
            for mailbox_message in batch.messages:
                notice = parse_delivery_notice(mailbox_message.raw)
                created, linked = await self._apply_notice(
                    session,
                    mailbox_message,
                    notice,
                    account_id=account_id,
                )
                processed += int(created)
                correlated += int(linked)
            proposed_history_id = batch.next_history_id
            if proposed_history_id is not None:
                if cursor.history_id is None:
                    cursor.history_id = proposed_history_id
                elif cursor.history_id.isdecimal() and proposed_history_id.isdecimal():
                    if int(proposed_history_id) > int(cursor.history_id):
                        cursor.history_id = proposed_history_id
                elif cursor.history_id == start_history_id:
                    cursor.history_id = proposed_history_id
            cursor.last_checked_at = datetime.now(UTC)
            await session.commit()
        return {
            "status": "ok",
            "fetched": len(batch.messages),
            "processed": processed,
            "correlated": correlated,
        }

    async def reconcile_all(self) -> dict[str, int | str]:
        if not self.settings.gmail_delivery_reconciliation_enabled:
            return {"status": "disabled", "fetched": 0, "processed": 0, "correlated": 0}
        async with self.session_factory() as session:
            await release_paused_email_contacts(
                session,
                now=datetime.now(UTC),
                pause_days=self.settings.email_failure_pause_days,
            )
            await session.commit()
        if self._mailbox is not None:
            return await self.reconcile(account_id=BOOTSTRAP_ADMIN_ACCOUNT_ID)
        async with self.session_factory() as session:
            credentials = list(
                (
                    await session.scalars(
                        select(OAuthCredential).where(OAuthCredential.provider == "gmail")
                    )
                ).all()
            )
        account_ids = [
            credential.account_id
            for credential in credentials
            if GMAIL_READONLY_SCOPE in credential.scopes
        ]
        if not account_ids:
            return {
                "status": "gmail_readonly_scope_required",
                "mailboxes": 0,
                "fetched": 0,
                "processed": 0,
                "correlated": 0,
            }
        fetched = 0
        processed = 0
        correlated = 0
        ok_mailboxes = 0
        reauth_mailboxes = 0
        temporary_mailboxes = 0
        for account_id in account_ids:
            result = await self.reconcile(account_id=account_id)
            fetched += int(result.get("fetched", 0))
            processed += int(result.get("processed", 0))
            correlated += int(result.get("correlated", 0))
            ok_mailboxes += int(result.get("status") == "ok")
            reauth_mailboxes += int(result.get("status") == GMAIL_REAUTH_REQUIRED_CODE)
            temporary_mailboxes += int(result.get("status") == "gmail_refresh_temporary_failure")
        return {
            "status": (
                "ok"
                if ok_mailboxes
                else GMAIL_REAUTH_REQUIRED_CODE
                if reauth_mailboxes
                else "gmail_refresh_temporary_failure"
                if temporary_mailboxes
                else "gmail_readonly_scope_required"
            ),
            "mailboxes": len(account_ids),
            "reauth_mailboxes": reauth_mailboxes,
            "temporary_mailboxes": temporary_mailboxes,
            "fetched": fetched,
            "processed": processed,
            "correlated": correlated,
        }

    async def audit_mailbox(
        self,
        *,
        account_id: UUID = BOOTSTRAP_ADMIN_ACCOUNT_ID,
        recipient_filter: str | None = None,
    ) -> dict[str, object]:
        """Inspect recent DSNs without advancing the cursor or mutating delivery state."""
        async with self.session_factory() as session:
            mailbox = await self._mailbox_for(session, account_id=account_id)
            if mailbox is None:
                return {"status": "gmail_readonly_scope_required", "matches": []}
        batch = await mailbox.fetch(
            start_history_id=None,
            max_results=self.settings.gmail_delivery_reconciliation_batch,
            monitor_days=self.settings.gmail_delivery_monitor_days,
        )
        rows: list[dict[str, object]] = []
        normalized_filter = _normalized_recipient(recipient_filter)
        async with self.session_factory() as session:
            for mailbox_message in batch.messages:
                notice = parse_delivery_notice(mailbox_message.raw)
                if not notice.is_dsn:
                    continue
                if normalized_filter and notice.final_recipient != normalized_filter:
                    continue
                delivery = await self._correlate(
                    session,
                    notice,
                    account_id=account_id,
                    thread_id=mailbox_message.thread_id,
                )
                classification = classify_smtp_failure(
                    notice.diagnostic, status=notice.status, action=notice.action
                )
                application = (
                    await session.get(Application, delivery.application_id)
                    if delivery is not None
                    else None
                )
                rows.append(
                    {
                        "application_id": (
                            str(application.id) if application is not None else None
                        ),
                        "employer_id": (
                            str(application.employer_id)
                            if application is not None and application.employer_id is not None
                            else None
                        ),
                        "delivery_id": str(delivery.id) if delivery is not None else None,
                        "recipient": notice.final_recipient,
                        "send_timestamp": (
                            delivery.created_at.isoformat() if delivery is not None else None
                        ),
                        "bounce_timestamp": notice.occurred_at.isoformat(),
                        "smtp_status": classification.smtp_status,
                        "action": notice.action,
                        "structured": notice.structured,
                        "classification": classification.failure_class,
                        "proposed_delivery_status": classification.status.value,
                        "proposed_contact_state": (
                            "healthy"
                            if classification.status is DeliveryStatus.DELIVERED
                            else "unchanged"
                            if classification.status is DeliveryStatus.PROVIDER_ACCEPTED
                            else "invalid"
                            if classification.failure_class
                            in {"recipient_not_found", "recipient_rejected"}
                            else "rejected"
                            if classification.permanent
                            else "transient_failure"
                        ),
                        "retryable": classification.retryable,
                    }
                )
        return {"status": "ok", "fetched": len(batch.messages), "matches": rows}


__all__ = [
    "BounceClassification",
    "EmailDeliveryReconciliationService",
    "GmailMailboxProvider",
    "MailboxBatch",
    "MailboxMessage",
    "ParsedDeliveryNotice",
    "classify_smtp_failure",
    "parse_delivery_notice",
]
