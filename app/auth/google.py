from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from google_auth_oauthlib.flow import Flow
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import OAuthAuthorizationRequest
from app.security.crypto import SecretBox, TokenDecryptionError
from app.settings import Settings

GOOGLE_IDENTITY_PROVIDER = "google_identity"
GOOGLE_OPENID_SCOPE = "openid"
GOOGLE_EMAIL_SCOPE = "email"
GOOGLE_USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
GOOGLE_IDENTITY_SCOPES = (GOOGLE_OPENID_SCOPE, GOOGLE_EMAIL_SCOPE)
IDENTITY_OAUTH_BINDING_COOKIE = "jobhunter_identity_oauth_binding"
IDENTITY_STATE_TTL_SECONDS = 10 * 60
IDENTITY_REQUEST_RETENTION = timedelta(days=1)


class GoogleIdentityError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "identity_oauth_failed",
        actor: str | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.actor = actor
        self.correlation_id = correlation_id


@dataclass(frozen=True, slots=True)
class GoogleIdentityStart:
    authorization_url: str
    binding_token: str
    request_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedGoogleIdentity:
    subject: str
    email: str


@dataclass(frozen=True, slots=True)
class GoogleIdentityExchange:
    identity: VerifiedGoogleIdentity
    actor: str
    request_id: UUID


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item for item in value.split() if item}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {item for item in value if isinstance(item, str) and item}
    return set()


def _canonical_identity_scopes(scopes: set[str]) -> set[str]:
    return {
        GOOGLE_EMAIL_SCOPE if item == GOOGLE_USERINFO_EMAIL_SCOPE else item for item in scopes
    }


def _accept_google_email_scope_alias(flow: Flow, warning: Warning) -> bool:
    token = getattr(warning, "token", None)
    if not isinstance(token, Mapping):
        return False
    token_scopes = _scope_values(token.get("scope"))
    if _canonical_identity_scopes(token_scopes) != set(GOOGLE_IDENTITY_SCOPES):
        return False
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return False
    oauth_session = getattr(flow, "oauth2session", None)
    if oauth_session is None:
        return False
    oauth_session.token = dict(token)
    return True


class GoogleIdentityService:
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

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.public_base_url.rstrip('/')}/api/v1/oauth/gmail/callback"

    @property
    def secure_cookie(self) -> bool:
        return self.settings.public_base_url.casefold().startswith("https://")

    def _require_box(self) -> SecretBox:
        if self._box is None:
            raise GoogleIdentityError(
                "TOKEN_ENCRYPTION_KEY is required",
                code="identity_oauth_not_configured",
            )
        return self._box

    def _flow(self, *, state: str, code_verifier: str) -> Flow:
        if self._client_id is None or self._client_secret is None:
            raise GoogleIdentityError(
                "Google OAuth client is not configured",
                code="identity_oauth_not_configured",
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
            scopes=list(GOOGLE_IDENTITY_SCOPES),
            state=state,
            code_verifier=code_verifier,
            autogenerate_code_verifier=False,
        )
        flow.redirect_uri = self.redirect_uri
        return flow

    async def create_authorization_request(
        self,
        session: AsyncSession,
        *,
        actor: str,
    ) -> GoogleIdentityStart:
        if not self.configured:
            raise GoogleIdentityError(
                "Google identity OAuth is not configured",
                code="identity_oauth_not_configured",
            )
        normalized_actor = actor.strip()
        if not normalized_actor or len(normalized_actor) > 255:
            raise GoogleIdentityError("invalid identity actor", code="invalid_identity_actor")
        now = datetime.now(UTC)
        await session.execute(
            delete(OAuthAuthorizationRequest).where(
                OAuthAuthorizationRequest.provider == GOOGLE_IDENTITY_PROVIDER,
                (OAuthAuthorizationRequest.expires_at < now - IDENTITY_REQUEST_RETENTION)
                | (
                    OAuthAuthorizationRequest.consumed_at.is_not(None)
                    & (
                        OAuthAuthorizationRequest.created_at
                        < now - IDENTITY_REQUEST_RETENTION
                    )
                ),
            )
        )
        state = secrets.token_urlsafe(32)
        binding_token = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        request = OAuthAuthorizationRequest(
            provider=GOOGLE_IDENTITY_PROVIDER,
            state_hash=_hash_token(state),
            binding_hash=_hash_token(binding_token),
            encrypted_code_verifier=self._require_box().encrypt(code_verifier),
            actor=normalized_actor,
            expires_at=now + timedelta(seconds=IDENTITY_STATE_TTL_SECONDS),
        )
        session.add(request)
        await session.flush()
        try:
            flow = self._flow(state=state, code_verifier=code_verifier)
            url, _ = flow.authorization_url(
                access_type="online",
                prompt="select_account",
                state=state,
                nonce=_hash_token(binding_token),
            )
        except GoogleIdentityError:
            raise
        except Exception as exc:
            raise GoogleIdentityError(
                "Google identity authorization URL generation failed",
                code="invalid_identity_authorization_url",
                actor=normalized_actor,
                correlation_id=request.id,
            ) from exc
        if not isinstance(url, str):
            raise GoogleIdentityError(
                "Google returned an invalid identity authorization URL",
                code="invalid_identity_authorization_url",
                actor=normalized_actor,
                correlation_id=request.id,
            )
        return GoogleIdentityStart(
            authorization_url=url,
            binding_token=binding_token,
            request_id=request.id,
            expires_at=request.expires_at,
        )

    async def is_identity_request(self, session: AsyncSession, *, state: str) -> bool:
        now = datetime.now(UTC)
        request_id = await session.scalar(
            select(OAuthAuthorizationRequest.id).where(
                OAuthAuthorizationRequest.provider == GOOGLE_IDENTITY_PROVIDER,
                OAuthAuthorizationRequest.state_hash == _hash_token(state),
                OAuthAuthorizationRequest.expires_at > now,
                OAuthAuthorizationRequest.consumed_at.is_(None),
            )
        )
        return request_id is not None

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
                OAuthAuthorizationRequest.provider == GOOGLE_IDENTITY_PROVIDER,
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
        row = result.one_or_none()
        if row is None:
            await session.rollback()
            raise GoogleIdentityError(
                "invalid, expired, or already used identity state",
                code="invalid_identity_oauth_state",
            )
        request_id, actor, encrypted_code_verifier = row
        await session.commit()
        try:
            code_verifier = self._require_box().decrypt(encrypted_code_verifier)
        except (GoogleIdentityError, TokenDecryptionError) as exc:
            raise GoogleIdentityError(
                "identity PKCE verifier could not be recovered",
                code="invalid_identity_pkce_verifier",
                actor=actor,
                correlation_id=request_id,
            ) from exc
        return request_id, actor, code_verifier

    async def exchange_callback(
        self,
        session: AsyncSession,
        *,
        authorization_response: str,
        state: str,
        binding_token: str,
    ) -> GoogleIdentityExchange:
        returned_state = parse_qs(urlsplit(authorization_response).query).get("state", [])
        if returned_state != [state]:
            raise GoogleIdentityError(
                "identity callback state is missing or ambiguous",
                code="invalid_identity_oauth_state",
            )
        expected_nonce = _hash_token(binding_token)
        request_id, actor, code_verifier = await self._consume_authorization_request(
            session,
            state=state,
            binding_token=binding_token,
        )
        callback_query = parse_qs(urlsplit(authorization_response).query)
        if callback_query.get("error") or len(callback_query.get("code", [])) != 1:
            raise GoogleIdentityError(
                "Google identity authorization was denied",
                code="identity_authorization_denied",
                actor=actor,
                correlation_id=request_id,
            )
        authorization_code = callback_query["code"][0]
        flow = self._flow(state=state, code_verifier=code_verifier)
        try:
            await asyncio.to_thread(flow.fetch_token, code=authorization_code)
        except Warning as exc:
            if not _accept_google_email_scope_alias(flow, exc):
                raise GoogleIdentityError(
                    "Google identity token exchange failed",
                    code="identity_token_exchange_failed",
                    actor=actor,
                    correlation_id=request_id,
                ) from exc
        except Exception as exc:
            raise GoogleIdentityError(
                "Google identity token exchange failed",
                code="identity_token_exchange_failed",
                actor=actor,
                correlation_id=request_id,
            ) from exc

        credentials: Any = flow.credentials
        granted_scopes = getattr(credentials, "granted_scopes", None) or getattr(
            credentials, "scopes", None
        )
        normalized_scopes = _scope_values(granted_scopes)
        if granted_scopes is not None:
            canonical = _canonical_identity_scopes(normalized_scopes)
            if canonical != set(GOOGLE_IDENTITY_SCOPES):
                raise GoogleIdentityError(
                    "Google identity scopes are not exactly the requested set",
                    code="identity_scope_mismatch",
                    actor=actor,
                    correlation_id=request_id,
                )
        raw_id_token = getattr(credentials, "id_token", None)
        if not isinstance(raw_id_token, str) or not raw_id_token:
            raise GoogleIdentityError(
                "Google did not return an identity token",
                code="invalid_google_identity",
                actor=actor,
                correlation_id=request_id,
            )
        assert self._client_id is not None
        try:
            claims = await asyncio.to_thread(
                google_id_token.verify_oauth2_token,
                raw_id_token,
                GoogleAuthRequest(),
                self._client_id.get_secret_value(),
            )
        except Exception as exc:
            raise GoogleIdentityError(
                "Google identity token validation failed",
                code="invalid_google_identity",
                actor=actor,
                correlation_id=request_id,
            ) from exc
        subject = claims.get("sub")
        email = claims.get("email")
        nonce = claims.get("nonce")
        if (
            not isinstance(subject, str)
            or not subject
            or not isinstance(email, str)
            or not email
            or claims.get("email_verified") is not True
            or not isinstance(nonce, str)
            or not secrets.compare_digest(nonce, expected_nonce)
        ):
            raise GoogleIdentityError(
                "Google identity claims are invalid",
                code="invalid_google_identity",
                actor=actor,
                correlation_id=request_id,
            )
        return GoogleIdentityExchange(
            identity=VerifiedGoogleIdentity(
                subject=subject,
                email=email.strip().casefold(),
            ),
            actor=actor,
            request_id=request_id,
        )


__all__ = [
    "GOOGLE_IDENTITY_PROVIDER",
    "GOOGLE_IDENTITY_SCOPES",
    "IDENTITY_OAUTH_BINDING_COOKIE",
    "IDENTITY_STATE_TTL_SECONDS",
    "GoogleIdentityError",
    "GoogleIdentityExchange",
    "GoogleIdentityService",
    "GoogleIdentityStart",
    "VerifiedGoogleIdentity",
]
