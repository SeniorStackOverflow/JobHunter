from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from oauthlib.oauth2 import WebApplicationClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import routes as admin_routes
from app.api import dependencies as api_dependencies
from app.api import routes as api_routes
from app.database.session import get_session
from app.email.oauth import (
    GMAIL_OAUTH_BINDING_COOKIE,
    GOOGLE_ADMIN_SCOPES,
    GOOGLE_USERINFO_EMAIL_SCOPE,
    GmailOAuthService,
)
from app.email.providers import GMAIL_SEND_SCOPE
from app.models.entities import AuditEvent, OAuthAuthorizationRequest, OAuthCredential
from app.security.auth import SessionSigner, hash_api_key
from app.settings import Settings

pytestmark = pytest.mark.integration

API_KEY = "gmail-oauth-route-test-key"


@dataclass
class OAuthApiContext:
    app: FastAPI
    client: httpx.AsyncClient
    session_factory: async_sessionmaker[AsyncSession]


class RouteFakeFlow:
    fetch_count = 0

    def __init__(self) -> None:
        self.credentials = SimpleNamespace(
            refresh_token="route-private-refresh-token",
            scopes=[GMAIL_SEND_SCOPE],
            granted_scopes=[GMAIL_SEND_SCOPE],
        )

    def fetch_token(self, *, code: str) -> None:
        assert code == "route-code"
        type(self).fetch_count += 1


class AdminRouteOAuthlibFlow:
    def __init__(
        self,
        scopes: list[str] | tuple[str, ...] | None = None,
        *,
        extra_scopes: tuple[str, ...] = (),
    ) -> None:
        self.requested_scopes = list(scopes or GOOGLE_ADMIN_SCOPES)
        self.extra_scopes = extra_scopes
        self.oauth2session = SimpleNamespace(token={})
        self.oauth_client = WebApplicationClient("route-client-id")

    def fetch_token(self, *, code: str) -> None:
        assert code == "admin-route-code"
        returned_scopes = [
            *self.requested_scopes,
            GOOGLE_USERINFO_EMAIL_SCOPE,
            *self.extra_scopes,
        ]
        response_body = json.dumps(
            {
                "access_token": "admin-route-private-access-token",
                "refresh_token": "admin-route-private-refresh-token",
                "id_token": "signed-google-id-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": " ".join(returned_scopes),
            }
        )
        self.oauth_client.parse_request_body_response(
            response_body,
            scope=self.requested_scopes,
        )

    @property
    def credentials(self) -> SimpleNamespace:
        token = self.oauth2session.token
        return SimpleNamespace(
            refresh_token=token.get("refresh_token"),
            scopes=self.requested_scopes,
            granted_scopes=token.get("scope"),
            id_token=token.get("id_token"),
        )


@pytest_asyncio.fixture
async def oauth_api(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[OAuthApiContext]:
    settings = Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        public_base_url="https://job-agent.example.test",
        secret_key="gmail-oauth-route-test-secret-over-32-characters",
        token_encryption_key="gmail-oauth-route-token-encryption-key",
        gmail_client_id="route-client-id",
        gmail_client_secret="route-client-secret",
        google_admin_emails=["owner@example.test"],
        admin_username="operator",
        mcp_api_keys_hashed=[hash_api_key(API_KEY)],
    )
    monkeypatch.setattr(api_dependencies, "get_settings", lambda: settings)
    monkeypatch.setattr(api_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(admin_routes, "get_settings", lambda: settings)

    application = FastAPI()
    application.include_router(api_routes.router)
    application.include_router(admin_routes.router)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sqlite_session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://job-agent.example.test",
        follow_redirects=False,
    ) as client:
        yield OAuthApiContext(application, client, sqlite_session_factory)


@pytest.mark.asyncio
async def test_gmail_oauth_rest_lifecycle_is_bound_audited_and_disconnectable(
    oauth_api: OAuthApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Authorization": f"Bearer {API_KEY}"}
    unauthenticated = await oauth_api.client.get("/api/v1/oauth/gmail/status")
    assert unauthenticated.status_code == 401

    started = await oauth_api.client.get("/api/v1/oauth/gmail/start", headers=headers)
    assert started.status_code == 302
    assert started.headers["cache-control"] == "no-store"
    cookie_header = started.headers["set-cookie"]
    assert GMAIL_OAUTH_BINDING_COOKIE in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header
    assert "Secure" in cookie_header
    location_query = parse_qs(urlsplit(started.headers["location"]).query)
    state = location_query["state"][0]
    assert location_query["scope"] == [GMAIL_SEND_SCOPE]
    assert "api-key" not in state

    async with oauth_api.session_factory() as session:
        request_row = await session.scalar(select(OAuthAuthorizationRequest))
        started_audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "oauth.gmail.started")
        )
        assert request_row is not None
        assert request_row.state_hash != state
        assert request_row.actor == "api-key"
        assert started_audit is not None
        audit_text = str(started_audit.sanitized_details)
        assert state not in audit_text
        assert "code_verifier" not in audit_text
        assert GMAIL_OAUTH_BINDING_COOKIE not in audit_text

    pending = await oauth_api.client.get("/api/v1/oauth/gmail/status", headers=headers)
    assert pending.status_code == 200
    assert pending.json()["configured"] is True
    assert pending.json()["connected"] is False
    assert pending.json()["identity_verified"] is False
    assert pending.json()["pending_authorizations"] == 1

    RouteFakeFlow.fetch_count = 0
    monkeypatch.setattr(
        GmailOAuthService,
        "_flow",
        lambda self, state=None, code_verifier=None: RouteFakeFlow(),
    )
    callback = await oauth_api.client.get(
        "/api/v1/oauth/gmail/callback",
        params={"code": "route-code", "state": state},
    )
    assert callback.status_code == 200
    assert callback.headers["cache-control"] == "no-store"
    assert callback.json()["status"] == "authorized"
    assert callback.json()["identity_verified"] is False
    assert RouteFakeFlow.fetch_count == 1

    replay = await oauth_api.client.get(
        "/api/v1/oauth/gmail/callback",
        params={"code": "route-code", "state": state},
    )
    assert replay.status_code == 400
    assert replay.json() == {"status": "failed", "error": "invalid_oauth_state"}
    assert RouteFakeFlow.fetch_count == 1

    connected = await oauth_api.client.get("/api/v1/oauth/gmail/status", headers=headers)
    assert connected.status_code == 200
    assert connected.json()["connected"] is True
    assert connected.json()["scopes"] == [GMAIL_SEND_SCOPE]
    assert connected.json()["identity_verified"] is False
    assert connected.json()["pending_authorizations"] == 0

    disconnected = await oauth_api.client.delete("/api/v1/oauth/gmail", headers=headers)
    assert disconnected.status_code == 200
    assert disconnected.json() == {
        "provider": "gmail",
        "connected": False,
        "was_connected": True,
        "remote_grant_revoked": False,
    }

    async with oauth_api.session_factory() as session:
        assert await session.scalar(select(OAuthCredential)) is None
        assert await session.scalar(select(OAuthAuthorizationRequest)) is None
        actions = set((await session.scalars(select(AuditEvent.action))).all())
        assert {
            "oauth.gmail.started",
            "oauth.gmail.connected",
            "oauth.gmail.connect_failed",
            "oauth.gmail.disconnected",
        } <= actions


@pytest.mark.asyncio
async def test_google_admin_login_verifies_allowlist_and_stores_gmail_token(
    oauth_api: OAuthApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await oauth_api.client.get("/admin/auth/google")
    assert started.status_code == 302
    location_query = parse_qs(urlsplit(started.headers["location"]).query)
    assert set(location_query["scope"][0].split()) == set(GOOGLE_ADMIN_SCOPES)
    assert location_query["prompt"] == ["consent select_account"]
    state = location_query["state"][0]
    nonce = location_query["nonce"][0]

    monkeypatch.setattr(
        GmailOAuthService,
        "_flow",
        lambda self, state=None, code_verifier=None, scopes=None: AdminRouteOAuthlibFlow(scopes),
    )

    async def verify_identity(_service: GmailOAuthService, raw_id_token: str) -> dict[str, object]:
        assert raw_id_token == "signed-google-id-token"
        return {
            "sub": "google-subject-123",
            "email": "Owner@Example.Test",
            "email_verified": True,
            "nonce": nonce,
        }

    monkeypatch.setattr(GmailOAuthService, "_verify_admin_id_token", verify_identity)
    callback = await oauth_api.client.get(
        "/api/v1/oauth/gmail/callback",
        params={"code": "admin-route-code", "state": state},
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/?view=overview&google=connected"
    session_cookie = oauth_api.client.cookies.get("job_agent_session")
    assert session_cookie is not None
    assert (
        SessionSigner("gmail-oauth-route-test-secret-over-32-characters").verify(session_cookie, 60)
        == "operator"
    )
    set_cookie = callback.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    # OAuth returns from a different site, so Strict would suppress this cookie on the
    # callback's immediate dashboard redirect and bounce the operator back to /login.
    assert "SameSite=lax" in set_cookie

    async with oauth_api.session_factory() as session:
        credential = await session.scalar(select(OAuthCredential))
        assert credential is not None
        assert credential.token_metadata == {
            "identity_verified": True,
            "identity_email": "owner@example.test",
            "identity_provider": "google",
        }
        actions = set((await session.scalars(select(AuditEvent.action))).all())
        assert "admin.login.google_started" in actions
        assert "admin.login.google" in actions


@pytest.mark.asyncio
async def test_google_admin_login_rejects_account_outside_allowlist(
    oauth_api: OAuthApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await oauth_api.client.get("/admin/auth/google")
    location_query = parse_qs(urlsplit(started.headers["location"]).query)
    state = location_query["state"][0]
    nonce = location_query["nonce"][0]
    monkeypatch.setattr(
        GmailOAuthService,
        "_flow",
        lambda self, state=None, code_verifier=None, scopes=None: AdminRouteOAuthlibFlow(scopes),
    )

    async def verify_identity(_service: GmailOAuthService, _raw_id_token: str) -> dict[str, object]:
        return {
            "sub": "attacker-subject",
            "email": "attacker@example.test",
            "email_verified": True,
            "nonce": nonce,
        }

    monkeypatch.setattr(GmailOAuthService, "_verify_admin_id_token", verify_identity)
    callback = await oauth_api.client.get(
        "/api/v1/oauth/gmail/callback",
        params={"code": "admin-route-code", "state": state},
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/login?oauth_error=admin_identity_not_allowed"
    assert oauth_api.client.cookies.get("job_agent_session") is None
    async with oauth_api.session_factory() as session:
        assert await session.scalar(select(OAuthCredential)) is None
        failed = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "admin.login.google_failed")
        )
        assert failed is not None


@pytest.mark.asyncio
async def test_google_admin_login_rejects_scope_change_beyond_email_alias(
    oauth_api: OAuthApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await oauth_api.client.get("/admin/auth/google")
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    monkeypatch.setattr(
        GmailOAuthService,
        "_flow",
        lambda self, state=None, code_verifier=None, scopes=None: AdminRouteOAuthlibFlow(
            scopes,
            extra_scopes=("https://www.googleapis.com/auth/drive.readonly",),
        ),
    )

    callback = await oauth_api.client.get(
        "/api/v1/oauth/gmail/callback",
        params={"code": "admin-route-code", "state": state},
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == "/login?oauth_error=token_exchange_failed"
    assert oauth_api.client.cookies.get("job_agent_session") is None
    async with oauth_api.session_factory() as session:
        assert await session.scalar(select(OAuthCredential)) is None
        failed = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "oauth.gmail.connect_failed")
        )
        assert failed is not None
        assert failed.sanitized_details["error_code"] == "token_exchange_failed"
