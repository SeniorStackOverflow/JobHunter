from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Protocol

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


@dataclass(frozen=True)
class PreparedEmail:
    application_id: str
    recipient: str
    subject: str
    body: str
    attachment_name: str
    attachment_mime_type: str
    attachment_data: bytes
    message_id: str


@dataclass(frozen=True)
class ProviderSendResult:
    message_id: str
    thread_id: str | None
    sanitized_response: dict[str, Any] = field(default_factory=dict)


class TemporaryDeliveryError(RuntimeError):
    """Provider explicitly rejected the request temporarily; retry can be safe."""


class DeliveryUnknownError(RuntimeError):
    """The request may have been accepted; automatic retry is forbidden."""


class PermanentDeliveryError(RuntimeError):
    """Provider rejected a request that must not be retried."""


class EmailProvider(Protocol):
    name: str

    async def send(self, message: PreparedEmail) -> ProviderSendResult: ...


class FakeGmailProvider:
    name = "fake_gmail"

    def __init__(self, failure_mode: str | None = None) -> None:
        self.outbox: list[PreparedEmail] = []
        self.failure_mode = failure_mode

    async def send(self, message: PreparedEmail) -> ProviderSendResult:
        if self.failure_mode == "temporary":
            raise TemporaryDeliveryError("fake temporary provider failure")
        if self.failure_mode == "unknown":
            raise DeliveryUnknownError("fake response was lost after dispatch")
        if self.failure_mode == "permanent":
            raise PermanentDeliveryError("fake permanent provider failure")
        self.outbox.append(message)
        identifier = f"fake-{message.application_id}"
        return ProviderSendResult(
            message_id=identifier,
            thread_id=f"thread-{message.application_id}",
            sanitized_response={"id": identifier, "threadId": f"thread-{message.application_id}"},
        )


class GmailApiProvider:
    name = "gmail"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> None:
        self._credentials = Credentials(  # type: ignore[no-untyped-call]
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
            client_id=client_id,
            client_secret=client_secret,
            scopes=[GMAIL_SEND_SCOPE],
        )

    @staticmethod
    def _raw_message(message: PreparedEmail) -> str:
        mime = EmailMessage()
        mime["To"] = message.recipient
        mime["Subject"] = message.subject
        mime["Message-ID"] = message.message_id
        mime.set_content(message.body)
        maintype, subtype = message.attachment_mime_type.split("/", maxsplit=1)
        mime.add_attachment(
            message.attachment_data,
            maintype=maintype,
            subtype=subtype,
            filename=message.attachment_name,
        )
        return base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")

    async def send(self, message: PreparedEmail) -> ProviderSendResult:
        raw = self._raw_message(message)

        def execute() -> dict[str, Any]:
            service = build("gmail", "v1", credentials=self._credentials, cache_discovery=False)
            result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return dict(result)

        try:
            response = await asyncio.to_thread(execute)
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status == 429:
                raise TemporaryDeliveryError(f"Gmail temporary HTTP {status}") from exc
            if status == 403 and any(
                marker in str(exc).casefold() for marker in ("ratelimit", "rate limit", "quota")
            ):
                raise TemporaryDeliveryError("Gmail rate limit") from exc
            if status in {500, 502, 503, 504}:
                # A server-side failure can arrive after Gmail accepted the MIME body.
                # Gmail does not expose an application-level idempotency key, so an
                # automatic retry would risk a duplicate application.
                raise DeliveryUnknownError(f"Gmail HTTP {status} outcome is unknown") from exc
            raise PermanentDeliveryError(f"Gmail rejected message with HTTP {status}") from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise DeliveryUnknownError("Gmail request outcome is unknown") from exc
        provider_id = response.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            raise DeliveryUnknownError("Gmail response did not contain a message id")
        thread_id = response.get("threadId")
        return ProviderSendResult(
            message_id=provider_id,
            thread_id=thread_id if isinstance(thread_id, str) else None,
            sanitized_response={"id": provider_id, "threadId": thread_id},
        )


def deterministic_message_id(application_id: str) -> str:
    digest = hashlib.sha256(application_id.encode("utf-8")).hexdigest()
    return f"<application-{digest}@job-agent.invalid>"
