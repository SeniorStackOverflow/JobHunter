from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.email.oauth import (
    GMAIL_DELIVERY_SCOPES,
    IDENTITY_UNVERIFIED_REASON,
    GmailOAuthError,
    GmailOAuthService,
)
from app.email.providers import GMAIL_SEND_SCOPE
from app.models.entities import Account, OAuthAuthorizationRequest, OAuthCredential
from app.models.enums import AccountRole, AccountStatus
from app.security.crypto import SecretBox
from app.settings import Settings


@dataclass
class FakeCredentials:
    refresh_token: str | None
    scopes: list[str] | None = None
    granted_scopes: list[str] | None = None


class FakeOAuthFlow:
    def __init__(
        self,
        refresh_token: str | None,
        *,
        granted_scopes: list[str] | None = None,
        fail_exchange: bool = False,
    ) -> None:
        scopes = list(GMAIL_DELIVERY_SCOPES) if granted_scopes is None else granted_scopes
        self.credentials = FakeCredentials(
            refresh_token=refresh_token,
            scopes=scopes,
            granted_scopes=scopes,
        )
        self.authorization_code: str | None = None
        self.fetch_count = 0
        self.fail_exchange = fail_exchange

    def fetch_token(self, *, code: str) -> None:
        self.fetch_count += 1
        self.authorization_code = code
        if self.fail_exchange:
            raise RuntimeError("provider exchange failed")


def oauth_settings() -> Settings:
    return Settings(
        environment="test",
        public_base_url="https://job-agent.example.test",
        secret_key="test-session-secret-with-more-than-32-characters",
        token_encryption_key="test-token-encryption-secret-32bytes",
        gmail_client_id="fixture-client-id",
        gmail_client_secret="fixture-client-secret",
    )


def callback_url(state: str) -> str:
    return "https://job-agent.example.test/api/v1/oauth/gmail/callback?" + urlencode(
        {"code": "fake-code", "state": state}
    )


@pytest.mark.asyncio
async def test_authorization_request_uses_exact_scope_opaque_state_and_server_side_pkce(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = iter(("opaque-state", "browser-binding", "v" * 64))
    monkeypatch.setattr("app.email.oauth.secrets.token_urlsafe", lambda _size: next(values))
    settings = oauth_settings()
    service = GmailOAuthService(settings)

    async with sqlite_session_factory() as session:
        started = await service.create_authorization_request(session, actor="api-key")
        await session.commit()
        stored = await session.scalar(select(OAuthAuthorizationRequest))

    query = parse_qs(urlsplit(started.authorization_url).query)
    assert query["scope"] == [" ".join(GMAIL_DELIVERY_SCOPES)]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert query["state"] == ["opaque-state"]
    assert "include_granted_scopes" not in query
    assert started.binding_token == "browser-binding"
    assert stored is not None
    assert stored.actor == "api-key"
    assert stored.state_hash == hashlib.sha256(b"opaque-state").hexdigest()
    assert stored.binding_hash == hashlib.sha256(b"browser-binding").hexdigest()
    assert stored.encrypted_code_verifier is not None
    assert b"v" * 64 not in stored.encrypted_code_verifier
    assert (
        SecretBox(settings.token_encryption_key or "").decrypt(stored.encrypted_code_verifier)
        == "v" * 64
    )


@pytest.mark.asyncio
async def test_oauth_callback_consumes_state_and_encrypts_refresh_token(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GmailOAuthService(oauth_settings())
    async with sqlite_session_factory() as session:
        started = await service.create_authorization_request(session, actor="api-key")
        await session.commit()
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    fake_flow = FakeOAuthFlow("private-refresh-token")
    monkeypatch.setattr(service, "_flow", lambda state=None, code_verifier=None: fake_flow)

    async with sqlite_session_factory() as session:
        result = await service.exchange_callback(
            session,
            authorization_response=callback_url(state),
            state=state,
            binding_token=started.binding_token,
        )
        await session.commit()
        stored_request = await session.get(OAuthAuthorizationRequest, started.request_id)
        assert isinstance(result.credential, OAuthCredential)
        assert result.actor == "api-key"
        assert result.credential.scopes == sorted(GMAIL_DELIVERY_SCOPES)
        assert result.credential.token_metadata["identity_verified"] is False
        assert b"private-refresh-token" not in result.credential.encrypted_refresh_token
        assert await service.get_refresh_token(session) == "private-refresh-token"
        assert stored_request is not None
        assert stored_request.consumed_at is not None
        assert stored_request.encrypted_code_verifier is None
        status = await service.get_status(session)

    assert fake_flow.authorization_code == "fake-code"
    assert fake_flow.fetch_count == 1
    assert status["connected"] is True
    assert status["scopes"] == sorted(GMAIL_DELIVERY_SCOPES)
    assert status["identity_verified"] is False
    assert status["identity_verification_reason"] == IDENTITY_UNVERIFIED_REASON
    assert status["pending_authorizations"] == 0


@pytest.mark.asyncio
async def test_legacy_send_only_grant_blocks_delivery_until_monitoring_consent(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = oauth_settings()
    service = GmailOAuthService(settings)
    async with sqlite_session_factory() as session:
        session.add(
            OAuthCredential(
                provider="gmail",
                encrypted_refresh_token=SecretBox(settings.token_encryption_key or "").encrypt(
                    "refresh-token"
                ),
                scopes=[GMAIL_SEND_SCOPE],
                token_metadata={},
            )
        )
        await session.commit()
        status = await service.get_status(session)
        with pytest.raises(GmailOAuthError) as missing_scope:
            await service.get_refresh_token(session, required_scopes=GMAIL_DELIVERY_SCOPES)

    assert status["connected"] is True
    assert status["delivery_ready"] is False
    assert status["monitoring_ready"] is False
    assert missing_scope.value.code == "gmail_reauthorization_required"


@pytest.mark.asyncio
async def test_wrong_browser_binding_does_not_consume_authorization_request(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GmailOAuthService(oauth_settings())
    async with sqlite_session_factory() as session:
        started = await service.create_authorization_request(session, actor="api-key")
        await session.commit()
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    fake_flow = FakeOAuthFlow("private-refresh-token")
    monkeypatch.setattr(service, "_flow", lambda state=None, code_verifier=None: fake_flow)

    async with sqlite_session_factory() as session:
        with pytest.raises(GmailOAuthError, match="invalid, expired, or already used"):
            await service.exchange_callback(
                session,
                authorization_response=callback_url(state),
                state=state,
                binding_token="wrong-browser-binding",
            )
        stored_request = await session.get(OAuthAuthorizationRequest, started.request_id)
        assert stored_request is not None
        assert stored_request.consumed_at is None
        assert stored_request.encrypted_code_verifier is not None

        await service.exchange_callback(
            session,
            authorization_response=callback_url(state),
            state=state,
            binding_token=started.binding_token,
        )
        await session.commit()

    assert fake_flow.fetch_count == 1


@pytest.mark.asyncio
async def test_oauth_state_is_one_time_and_replay_never_reaches_provider(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GmailOAuthService(oauth_settings())
    async with sqlite_session_factory() as session:
        started = await service.create_authorization_request(session, actor="api-key")
        await session.commit()
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    fake_flow = FakeOAuthFlow("private-refresh-token")
    monkeypatch.setattr(service, "_flow", lambda state=None, code_verifier=None: fake_flow)

    async with sqlite_session_factory() as session:
        await service.exchange_callback(
            session,
            authorization_response=callback_url(state),
            state=state,
            binding_token=started.binding_token,
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        with pytest.raises(GmailOAuthError) as replay:
            await service.exchange_callback(
                session,
                authorization_response=callback_url(state),
                state=state,
                binding_token=started.binding_token,
            )

    assert replay.value.code == "invalid_oauth_state"
    assert fake_flow.fetch_count == 1


@pytest.mark.asyncio
async def test_expired_oauth_state_is_rejected_before_provider_exchange(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GmailOAuthService(oauth_settings())
    async with sqlite_session_factory() as session:
        started = await service.create_authorization_request(session, actor="api-key")
        stored = await session.get(OAuthAuthorizationRequest, started.request_id)
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    fake_flow = FakeOAuthFlow("private-refresh-token")
    monkeypatch.setattr(service, "_flow", lambda state=None, code_verifier=None: fake_flow)

    async with sqlite_session_factory() as session:
        with pytest.raises(GmailOAuthError) as expired:
            await service.exchange_callback(
                session,
                authorization_response=callback_url(state),
                state=state,
                binding_token=started.binding_token,
            )

    assert expired.value.code == "invalid_oauth_state"
    assert fake_flow.fetch_count == 0


@pytest.mark.asyncio
async def test_provider_failure_still_consumes_state_and_clears_pkce(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GmailOAuthService(oauth_settings())
    async with sqlite_session_factory() as session:
        started = await service.create_authorization_request(session, actor="api-key")
        await session.commit()
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    fake_flow = FakeOAuthFlow("private-refresh-token", fail_exchange=True)
    monkeypatch.setattr(service, "_flow", lambda state=None, code_verifier=None: fake_flow)

    async with sqlite_session_factory() as session:
        with pytest.raises(GmailOAuthError) as failed:
            await service.exchange_callback(
                session,
                authorization_response=callback_url(state),
                state=state,
                binding_token=started.binding_token,
            )
        stored = await session.get(OAuthAuthorizationRequest, started.request_id)
        assert stored is not None
        assert stored.consumed_at is not None
        assert stored.encrypted_code_verifier is None

    assert failed.value.code == "token_exchange_failed"
    assert failed.value.actor == "api-key"
    assert fake_flow.fetch_count == 1


@pytest.mark.asyncio
async def test_unexpected_scope_grant_is_rejected_and_not_persisted(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GmailOAuthService(oauth_settings())
    async with sqlite_session_factory() as session:
        started = await service.create_authorization_request(session, actor="api-key")
        await session.commit()
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    fake_flow = FakeOAuthFlow(
        "private-refresh-token",
        granted_scopes=[*GMAIL_DELIVERY_SCOPES, "openid"],
    )
    monkeypatch.setattr(service, "_flow", lambda state=None, code_verifier=None: fake_flow)

    async with sqlite_session_factory() as session:
        with pytest.raises(GmailOAuthError) as rejected:
            await service.exchange_callback(
                session,
                authorization_response=callback_url(state),
                state=state,
                binding_token=started.binding_token,
            )
        assert await session.scalar(select(OAuthCredential)) is None

    assert rejected.value.code == "unexpected_scope_grant"
    assert fake_flow.fetch_count == 1


@pytest.mark.asyncio
async def test_disconnect_removes_credential_and_invalidates_pending_requests(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = oauth_settings()
    service = GmailOAuthService(settings)
    async with sqlite_session_factory() as session:
        await service.create_authorization_request(session, actor="api-key")
        session.add(
            OAuthCredential(
                provider="gmail",
                encrypted_refresh_token=SecretBox(settings.token_encryption_key or "").encrypt(
                    "refresh-token"
                ),
                scopes=[GMAIL_SEND_SCOPE],
                token_metadata={},
            )
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        assert await service.disconnect(session) is True
        await session.commit()
        assert await session.scalar(select(OAuthCredential)) is None
        assert await session.scalar(select(OAuthAuthorizationRequest)) is None
        assert await service.disconnect(session) is False


@pytest.mark.asyncio
async def test_disconnect_cancels_pending_callback_before_provider_exchange(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GmailOAuthService(oauth_settings())
    async with sqlite_session_factory() as session:
        started = await service.create_authorization_request(session, actor="api-key")
        await session.commit()
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    fake_flow = FakeOAuthFlow("private-refresh-token")
    monkeypatch.setattr(service, "_flow", lambda state=None, code_verifier=None: fake_flow)

    async with sqlite_session_factory() as session:
        assert await service.disconnect(session) is False
        await session.commit()

    async with sqlite_session_factory() as session:
        with pytest.raises(GmailOAuthError) as cancelled:
            await service.exchange_callback(
                session,
                authorization_response=callback_url(state),
                state=state,
                binding_token=started.binding_token,
            )

    assert cancelled.value.code == "invalid_oauth_state"
    assert fake_flow.fetch_count == 0


@pytest.mark.asyncio
async def test_gmail_credentials_and_reauth_are_isolated_by_account(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = GmailOAuthService(oauth_settings())
    async with sqlite_session_factory() as session:
        first = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        second = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        session.add_all([first, second])
        await session.flush()
        first_start = await service.create_authorization_request(
            session,
            actor=f"user-gmail:{first.id}",
            account_id=first.id,
        )
        second_start = await service.create_authorization_request(
            session,
            actor=f"user-gmail:{second.id}",
            account_id=second.id,
        )
        await session.commit()
        first_id = first.id
        second_id = second.id

    first_state = parse_qs(urlsplit(first_start.authorization_url).query)["state"][0]
    second_state = parse_qs(urlsplit(second_start.authorization_url).query)["state"][0]
    flows = [
        FakeOAuthFlow("refresh-first"),
        FakeOAuthFlow("refresh-second"),
    ]
    monkeypatch.setattr(
        service,
        "_flow",
        lambda state=None, code_verifier=None, scopes=None: flows.pop(0),
    )

    async with sqlite_session_factory() as session:
        first_result = await service.exchange_callback(
            session,
            authorization_response=callback_url(first_state),
            state=first_state,
            binding_token=first_start.binding_token,
        )
        await session.commit()
        assert first_result.credential.account_id == first_id

    async with sqlite_session_factory() as session:
        second_result = await service.exchange_callback(
            session,
            authorization_response=callback_url(second_state),
            state=second_state,
            binding_token=second_start.binding_token,
        )
        await session.commit()
        assert second_result.credential.account_id == second_id

    async with sqlite_session_factory() as session:
        assert await service.get_refresh_token(session, account_id=first_id) == "refresh-first"
        assert await service.get_refresh_token(session, account_id=second_id) == "refresh-second"
        await service.mark_reauthorization_required(
            session,
            account_id=first_id,
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        first_status = await service.get_status(session, account_id=first_id)
        second_status = await service.get_status(session, account_id=second_id)
        assert first_status["reauth_required"] is True
        assert first_status["delivery_ready"] is False
        assert second_status["reauth_required"] is False
        assert second_status["delivery_ready"] is True


@pytest.mark.asyncio
async def test_disconnect_deletes_only_target_accounts_gmail_credential(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = oauth_settings()
    service = GmailOAuthService(settings)
    box = SecretBox(settings.token_encryption_key or "")
    async with sqlite_session_factory() as session:
        first = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        second = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        session.add_all([first, second])
        await session.flush()
        session.add_all(
            [
                OAuthCredential(
                    account_id=first.id,
                    provider="gmail",
                    encrypted_refresh_token=box.encrypt("first"),
                    scopes=list(GMAIL_DELIVERY_SCOPES),
                    token_metadata={},
                ),
                OAuthCredential(
                    account_id=second.id,
                    provider="gmail",
                    encrypted_refresh_token=box.encrypt("second"),
                    scopes=list(GMAIL_DELIVERY_SCOPES),
                    token_metadata={},
                ),
            ]
        )
        await session.commit()
        first_id = first.id
        second_id = second.id

    async with sqlite_session_factory() as session:
        assert await service.disconnect(session, account_id=first_id) is True
        await session.commit()

    async with sqlite_session_factory() as session:
        assert (await service.get_status(session, account_id=first_id))["connected"] is False
        assert (await service.get_status(session, account_id=second_id))["connected"] is True
