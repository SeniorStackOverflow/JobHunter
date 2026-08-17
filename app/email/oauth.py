from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from google_auth_oauthlib.flow import Flow
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.email.providers import GMAIL_SEND_SCOPE
from app.models.entities import OAuthAuthorizationRequest, OAuthCredential
from app.security.crypto import SecretBox, TokenDecryptionError
from app.settings import Settings

GMAIL_PROVIDER = "gmail"
GMAIL_OAUTH_BINDING_COOKIE = "job_agent_gmail_oauth_binding"
OAUTH_STATE_TTL_SECONDS = 10 * 60
OAUTH_REQUEST_RETENTION = timedelta(days=1)
IDENTITY_UNVERIFIED_REASON = (
    "The gmail.send scope does not permit an independent mailbox identity lookup."
)


class GmailOAuthError(ValueError):
    """OAuth state or credential exchange failed safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "oauth_failed",
        actor: str | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.actor = actor
        self.correlation_id = correlation_id


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationStart:
    authorization_url: str
    binding_token: str
    request_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OAuthExchangeResult:
    credential: OAuthCredential
    actor: str
    request_id: UUID


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class GmailOAuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client_id = settings.gmail_client_id
        self._client_secret = settings.gmail_client_secret
        self._box = (
            SecretBox(settings.token_encryption_key)
            if settings.token_encryption_key is not None
            else None
        )

    @property
    def configured(self) -> bool:
        return (
            self._client_id is not None
            and self._client_secret is not None
            and self._box is not None
        )

    def _require_box(self) -> SecretBox:
        if self._box is None:
            raise GmailOAuthError("TOKEN_ENCRYPTION_KEY is required", code="oauth_not_configured")
        return self._box

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.public_base_url.rstrip('/')}/api/v1/oauth/gmail/callback"

    @property
    def secure_cookie(self) -> bool:
        return self.settings.public_base_url.casefold().startswith("https://")

    def _flow(
        self,
        state: str | None = None,
        *,
        code_verifier: str | None = None,
    ) -> Flow:
        if self._client_id is None or self._client_secret is None:
            raise GmailOAuthError(
                "Gmail OAuth client is not configured", code="oauth_not_configured"
            )
        config = {
            "web": {
                "client_id": self._client_id.get_secret_value(),
                "client_secret": self._client_secret.get_secret_value(),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [self.redirect_uri],
            }
        }
        flow = Flow.from_client_config(
            config,
            scopes=[GMAIL_SEND_SCOPE],
            state=state,
            code_verifier=code_verifier,
            autogenerate_code_verifier=code_verifier is None,
        )
        flow.redirect_uri = self.redirect_uri
        return flow

    async def create_authorization_request(
        self, session: AsyncSession, *, actor: str
    ) -> OAuthAuthorizationStart:
        normalized_actor = actor.strip()
        if not normalized_actor or len(normalized_actor) > 255:
            raise GmailOAuthError("invalid OAuth actor", code="invalid_actor")

        now = datetime.now(UTC)
        await session.execute(
            delete(OAuthAuthorizationRequest).where(
                OAuthAuthorizationRequest.provider == GMAIL_PROVIDER,
                (OAuthAuthorizationRequest.expires_at < now - OAUTH_REQUEST_RETENTION)
                | (
                    OAuthAuthorizationRequest.consumed_at.is_not(None)
                    & (OAuthAuthorizationRequest.created_at < now - OAUTH_REQUEST_RETENTION)
                ),
            )
        )

        state = secrets.token_urlsafe(32)
        binding_token = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        expires_at = now + timedelta(seconds=OAUTH_STATE_TTL_SECONDS)
        authorization_request = OAuthAuthorizationRequest(
            provider=GMAIL_PROVIDER,
            state_hash=_hash_token(state),
            binding_hash=_hash_token(binding_token),
            encrypted_code_verifier=self._require_box().encrypt(code_verifier),
            actor=normalized_actor,
            expires_at=expires_at,
        )
        session.add(authorization_request)
        await session.flush()

        try:
            flow = self._flow(state=state, code_verifier=code_verifier)
            url, _ = flow.authorization_url(
                access_type="offline",
                prompt="consent",
                state=state,
            )
        except GmailOAuthError as exc:
            raise GmailOAuthError(
                "Gmail OAuth client is not configured",
                code=exc.code,
                actor=normalized_actor,
                correlation_id=authorization_request.id,
            ) from exc
        except Exception as exc:
            raise GmailOAuthError(
                "OAuth authorization URL generation failed",
                code="invalid_authorization_url",
                actor=normalized_actor,
                correlation_id=authorization_request.id,
            ) from exc
        if not isinstance(url, str):
            raise GmailOAuthError(
                "OAuth provider returned an invalid authorization URL",
                code="invalid_authorization_url",
                actor=normalized_actor,
                correlation_id=authorization_request.id,
            )
        return OAuthAuthorizationStart(
            authorization_url=url,
            binding_token=binding_token,
            request_id=authorization_request.id,
            expires_at=expires_at,
        )

    async def _consume_authorization_request(
        self,
        session: AsyncSession,
        *,
        state: str,
        binding_token: str,
    ) -> tuple[UUID, str, str]:
        now = datetime.now(UTC)
        result = await session.execute(
            update(OAuthAuthorizationRequest)
            .where(
                OAuthAuthorizationRequest.provider == GMAIL_PROVIDER,
                OAuthAuthorizationRequest.state_hash == _hash_token(state),
                OAuthAuthorizationRequest.binding_hash == _hash_token(binding_token),
                OAuthAuthorizationRequest.expires_at > now,
                OAuthAuthorizationRequest.consumed_at.is_(None),
                OAuthAuthorizationRequest.encrypted_code_verifier.is_not(None),
            )
            .values(consumed_at=now)
            .returning(
                OAuthAuthorizationRequest.id,
                OAuthAuthorizationRequest.actor,
                OAuthAuthorizationRequest.encrypted_code_verifier,
            )
            .execution_options(synchronize_session=False)
        )
        consumed = result.one_or_none()
        if consumed is None:
            await session.rollback()
            raise GmailOAuthError(
                "invalid, expired, or already used OAuth state",
                code="invalid_oauth_state",
            )

        request_id, actor, encrypted_code_verifier = consumed
        # Commit the one-time transition before any external token request. A provider
        # timeout or a replay can therefore never reuse the authorization request.
        await session.commit()
        try:
            code_verifier = self._require_box().decrypt(encrypted_code_verifier)
        except GmailOAuthError as exc:
            await self._clear_code_verifier(session, request_id)
            raise GmailOAuthError(
                "OAuth token encryption is not configured",
                code=exc.code,
                actor=actor,
                correlation_id=request_id,
            ) from exc
        except (TokenDecryptionError, UnicodeDecodeError) as exc:
            await self._clear_code_verifier(session, request_id)
            raise GmailOAuthError(
                "stored PKCE verifier failed authentication",
                code="invalid_pkce_verifier",
                actor=actor,
                correlation_id=request_id,
            ) from exc

        await self._clear_code_verifier(session, request_id)
        if not 43 <= len(code_verifier) <= 128:
            raise GmailOAuthError(
                "stored PKCE verifier is invalid",
                code="invalid_pkce_verifier",
                actor=actor,
                correlation_id=request_id,
            )
        return request_id, actor, code_verifier

    @staticmethod
    async def _clear_code_verifier(session: AsyncSession, request_id: UUID) -> None:
        await session.execute(
            update(OAuthAuthorizationRequest)
            .where(OAuthAuthorizationRequest.id == request_id)
            .values(encrypted_code_verifier=None)
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    @staticmethod
    async def _lock_consumed_request(
        session: AsyncSession, *, request_id: UUID, actor: str
    ) -> None:
        stored_request_id = await session.scalar(
            select(OAuthAuthorizationRequest.id)
            .where(
                OAuthAuthorizationRequest.id == request_id,
                OAuthAuthorizationRequest.provider == GMAIL_PROVIDER,
                OAuthAuthorizationRequest.consumed_at.is_not(None),
            )
            .with_for_update()
        )
        if stored_request_id is None:
            raise GmailOAuthError(
                "OAuth authorization was disconnected while completing",
                code="authorization_cancelled",
                actor=actor,
                correlation_id=request_id,
            )

    async def exchange_callback(
        self,
        session: AsyncSession,
        *,
        authorization_response: str,
        state: str,
        binding_token: str,
    ) -> OAuthExchangeResult:
        returned_state = parse_qs(urlsplit(authorization_response).query).get("state", [])
        if returned_state != [state]:
            raise GmailOAuthError(
                "OAuth callback state is missing or ambiguous", code="invalid_oauth_state"
            )

        request_id, actor, code_verifier = await self._consume_authorization_request(
            session, state=state, binding_token=binding_token
        )
        callback_query = parse_qs(urlsplit(authorization_response).query)
        if callback_query.get("error") or len(callback_query.get("code", [])) != 1:
            raise GmailOAuthError(
                "OAuth provider did not return one authorization code",
                code="authorization_denied",
                actor=actor,
                correlation_id=request_id,
            )

        authorization_code = callback_query["code"][0]
        try:
            flow = self._flow(state=state, code_verifier=code_verifier)
            await asyncio.to_thread(
                flow.fetch_token,
                code=authorization_code,
            )
        except GmailOAuthError as exc:
            raise GmailOAuthError(
                "Gmail OAuth client is not configured",
                code=exc.code,
                actor=actor,
                correlation_id=request_id,
            ) from exc
        except Exception as exc:
            raise GmailOAuthError(
                "OAuth token exchange failed",
                code="token_exchange_failed",
                actor=actor,
                correlation_id=request_id,
            ) from exc

        credentials: Any = flow.credentials
        granted_scopes = getattr(credentials, "granted_scopes", None) or getattr(
            credentials, "scopes", None
        )
        normalized_scopes = (
            {item for item in granted_scopes.split() if item}
            if isinstance(granted_scopes, str)
            else set(granted_scopes or [])
        )
        if granted_scopes is not None and GMAIL_SEND_SCOPE not in normalized_scopes:
            raise GmailOAuthError(
                "Google did not grant gmail.send",
                code="required_scope_missing",
                actor=actor,
                correlation_id=request_id,
            )
        if normalized_scopes - {GMAIL_SEND_SCOPE}:
            raise GmailOAuthError(
                "Google returned scopes that were not requested",
                code="unexpected_scope_grant",
                actor=actor,
                correlation_id=request_id,
            )
        refresh_token = credentials.refresh_token
        if not isinstance(refresh_token, str) or not refresh_token:
            raise GmailOAuthError(
                "Google did not issue a refresh token",
                code="refresh_token_missing",
                actor=actor,
                correlation_id=request_id,
            )

        token_metadata = {
            "identity_verified": False,
            "identity_verification_reason": IDENTITY_UNVERIFIED_REASON,
        }
        # Serializes local disconnect against credential persistence. Disconnect deletes
        # authorization rows first and credentials second, so it wins safely even when a
        # Google token exchange was already in flight.
        await self._lock_consumed_request(session, request_id=request_id, actor=actor)
        credential = await session.scalar(
            select(OAuthCredential)
            .where(OAuthCredential.provider == GMAIL_PROVIDER)
            .with_for_update()
        )
        if credential is None:
            credential = OAuthCredential(
                provider=GMAIL_PROVIDER,
                encrypted_refresh_token=self._require_box().encrypt(refresh_token),
                scopes=[GMAIL_SEND_SCOPE],
                token_metadata=token_metadata,
            )
            session.add(credential)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                await self._lock_consumed_request(session, request_id=request_id, actor=actor)
                credential = await session.scalar(
                    select(OAuthCredential)
                    .where(OAuthCredential.provider == GMAIL_PROVIDER)
                    .with_for_update()
                )
                if credential is None:
                    raise GmailOAuthError(
                        "OAuth credential could not be stored",
                        code="credential_storage_failed",
                        actor=actor,
                        correlation_id=request_id,
                    ) from exc
        credential.encrypted_refresh_token = self._require_box().encrypt(refresh_token)
        credential.scopes = [GMAIL_SEND_SCOPE]
        credential.token_metadata = token_metadata
        await session.flush()
        return OAuthExchangeResult(
            credential=credential,
            actor=actor,
            request_id=request_id,
        )

    async def get_status(self, session: AsyncSession) -> dict[str, Any]:
        credential = await session.scalar(
            select(OAuthCredential).where(OAuthCredential.provider == GMAIL_PROVIDER)
        )
        pending_count = await session.scalar(
            select(func.count(OAuthAuthorizationRequest.id)).where(
                OAuthAuthorizationRequest.provider == GMAIL_PROVIDER,
                OAuthAuthorizationRequest.expires_at > datetime.now(UTC),
                OAuthAuthorizationRequest.consumed_at.is_(None),
            )
        )
        return {
            "provider": GMAIL_PROVIDER,
            "configured": self.configured,
            "connected": credential is not None,
            "scopes": list(credential.scopes) if credential is not None else [],
            "identity_verified": False,
            "identity_verification_reason": IDENTITY_UNVERIFIED_REASON,
            "connected_at": credential.created_at if credential is not None else None,
            "updated_at": credential.updated_at if credential is not None else None,
            "pending_authorizations": int(pending_count or 0),
        }

    async def disconnect(self, session: AsyncSession) -> bool:
        # This order pairs with _lock_consumed_request: an in-flight callback either
        # observes the deletion and aborts, or commits first and is then deleted here.
        await session.execute(
            delete(OAuthAuthorizationRequest).where(
                OAuthAuthorizationRequest.provider == GMAIL_PROVIDER
            )
        )
        deleted_credential = await session.execute(
            delete(OAuthCredential)
            .where(OAuthCredential.provider == GMAIL_PROVIDER)
            .returning(OAuthCredential.id)
        )
        await session.flush()
        return deleted_credential.scalar_one_or_none() is not None

    async def get_refresh_token(self, session: AsyncSession) -> str:
        credential = await session.scalar(
            select(OAuthCredential).where(OAuthCredential.provider == GMAIL_PROVIDER)
        )
        if credential is None:
            raise GmailOAuthError(
                "Gmail authorization is not configured", code="gmail_not_connected"
            )
        return self._require_box().decrypt(credential.encrypted_refresh_token)
