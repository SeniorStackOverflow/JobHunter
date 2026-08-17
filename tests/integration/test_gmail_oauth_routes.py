from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import dependencies as api_dependencies
from app.api import routes as api_routes
from app.database.session import get_session
from app.email.oauth import GMAIL_OAUTH_BINDING_COOKIE, GmailOAuthService
from app.email.providers import GMAIL_SEND_SCOPE
from app.models.entities import AuditEvent, OAuthAuthorizationRequest, OAuthCredential
from app.security.auth import hash_api_key
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
        mcp_api_keys_hashed=[hash_api_key(API_KEY)],
    )
    monkeypatch.setattr(api_dependencies, "get_settings", lambda: settings)
    monkeypatch.setattr(api_routes, "get_settings", lambda: settings)

    application = FastAPI()
    application.include_router(api_routes.router)

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
