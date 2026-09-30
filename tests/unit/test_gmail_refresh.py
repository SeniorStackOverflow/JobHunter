from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import pytest
from google.auth.exceptions import RefreshError
from sqlalchemy import select

from app.email import delivery as delivery_module
from app.email import providers
from app.email.delivery import (
    EmailDeliveryReconciliationService,
    GmailMailboxProvider,
    MailboxBatch,
)
from app.email.oauth import GMAIL_DELIVERY_SCOPES, GmailOAuthService
from app.email.providers import (
    GMAIL_REAUTH_REQUIRED_CODE,
    GmailApiProvider,
    GmailReauthorizationRequired,
    PreparedEmail,
    TemporaryDeliveryError,
)
from app.models.constants import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.models.entities import Account, EmailMailboxCursor, OAuthCredential
from app.models.enums import AccountRole, AccountStatus
from app.security.crypto import SecretBox
from app.settings import Settings


class GmailService:
    def __init__(self, execute: Any) -> None:
        self.execute = execute

    def users(self) -> GmailService:
        return self

    def getProfile(self, **_kwargs: Any) -> GmailService:
        return self

    def messages(self) -> GmailService:
        return self

    def send(self, **_kwargs: Any) -> GmailService:
        return self

    def list(self, **_kwargs: Any) -> GmailService:
        return self


def message() -> PreparedEmail:
    return PreparedEmail(
        "fixture-app",
        "recipient@example.test",
        "Fixture",
        "Body",
        "resume.pdf",
        "application/pdf",
        b"%PDF-1.7\nfixture",
        "<fixture@example.test>",
    )


@pytest.mark.asyncio
async def test_gmail_sdk_refreshes_access_token_without_browser_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_requests = []
    sent = []

    def fake_token_request(url: str, *, method: str, body: bytes, **_kwargs: Any) -> Any:
        assert url == "https://oauth2.googleapis.com/token"
        assert method == "POST"
        form = parse_qs(body.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["stored-fixture-refresh-token"]
        token_requests.append(form)
        return SimpleNamespace(
            status=200,
            data=json.dumps(
                {
                    "access_token": "refreshed-fixture-access-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            ).encode(),
        )

    def build(_api: str, _version: str, *, credentials: Any, **_kwargs: Any) -> GmailService:
        def execute() -> dict[str, str]:
            headers: dict[str, str] = {}
            credentials.before_request(
                fake_token_request, "POST", "https://gmail.googleapis.com/fixture", headers
            )
            assert headers["authorization"] == "Bearer refreshed-fixture-access-token"
            sent.append(True)
            return {"id": "fixture-provider-id"}

        return GmailService(execute)

    monkeypatch.setattr(providers, "build", build)
    provider = GmailApiProvider(
        client_id="fixture", client_secret="fixture", refresh_token="stored-fixture-refresh-token"
    )
    assert (await provider.send(message())).message_id == "fixture-provider-id"
    assert (await provider.send(message())).message_id == "fixture-provider-id"
    assert len(token_requests) == 1 and len(sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable", [False, True])
@pytest.mark.parametrize("mailbox", [False, True])
async def test_refresh_errors_require_consent_only_when_permanent(
    monkeypatch: pytest.MonkeyPatch,
    retryable: bool,
    mailbox: bool,
) -> None:
    def execute() -> None:
        raise RefreshError("provider details must not leak", retryable=retryable)

    monkeypatch.setattr(
        delivery_module if mailbox else providers,
        "build",
        lambda *_args, **_kwargs: GmailService(execute),
    )
    expected = TemporaryDeliveryError if retryable else GmailReauthorizationRequired
    with pytest.raises(expected) as failure:
        if mailbox:
            await GmailMailboxProvider(
                client_id="fixture", client_secret="fixture", refresh_token="fixture"
            ).fetch(
                start_history_id=None,
                max_results=1,
                monitor_days=14,
            )
        else:
            await GmailApiProvider(
                client_id="fixture", client_secret="fixture", refresh_token="fixture"
            ).send(message())
    assert "provider details" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable", [False, True])
async def test_mailbox_refresh_failure_preserves_cursor_and_only_marks_affected_account(
    sqlite_session_factory: Any,
    retryable: bool,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        gmail_delivery_reconciliation_enabled=True,
        token_encryption_key="fixture-token-encryption-key-long-enough",
    )
    async with sqlite_session_factory() as session:
        other = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        session.add(other)
        await session.flush()
        other_id = other.id
        for account_id in (BOOTSTRAP_ADMIN_ACCOUNT_ID, other_id):
            session.add(
                OAuthCredential(
                    account_id=account_id,
                    provider="gmail",
                    encrypted_refresh_token=SecretBox(settings.token_encryption_key or "").encrypt(
                        "fixture"
                    ),
                    scopes=list(GMAIL_DELIVERY_SCOPES),
                )
            )
            session.add(
                EmailMailboxCursor(account_id=account_id, provider="gmail", history_id="100")
            )
        await session.commit()

    class FailedMailbox:
        async def fetch(self, **_kwargs: Any) -> MailboxBatch:
            raise (
                TemporaryDeliveryError("temporary")
                if retryable
                else GmailReauthorizationRequired("reauthorize")
            )

    service = EmailDeliveryReconciliationService(settings, sqlite_session_factory, FailedMailbox())
    result = await service.reconcile()
    assert result["status"] == (
        "gmail_refresh_temporary_failure" if retryable else GMAIL_REAUTH_REQUIRED_CODE
    )
    async with sqlite_session_factory() as session:
        cursor = await session.get(EmailMailboxCursor, (BOOTSTRAP_ADMIN_ACCOUNT_ID, "gmail"))
        assert cursor is not None and cursor.history_id == "100"
        own = await GmailOAuthService(settings).get_status(session)
        other = await GmailOAuthService(settings).get_status(session, account_id=other_id)
        assert own["reauth_required"] is (not retryable)
        assert other["reauth_required"] is False
        assert (
            await session.scalar(
                select(EmailMailboxCursor.history_id).where(
                    EmailMailboxCursor.account_id == other_id
                )
            )
            == "100"
        )
