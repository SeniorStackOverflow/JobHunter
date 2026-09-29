from __future__ import annotations

import asyncio
import re
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts import InviteService
from app.admin import router as admin_router
from app.admin import routes as admin_routes
from app.api import routes as api_routes
from app.auth import google as google_auth
from app.auth import routes as auth_routes
from app.auth.google import GOOGLE_IDENTITY_SCOPES, GoogleIdentityService
from app.database.session import get_session
from app.email.oauth import GMAIL_DELIVERY_SCOPES, GmailOAuthService
from app.models.constants import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.models.entities import (
    Account,
    AccountIdentity,
    Invite,
    JobPreference,
    OAuthCredential,
    UserProfile,
)
from app.models.enums import AccountRole, AccountStatus, ProfileStatus
from app.security.auth import AccountSessionSigner, SessionSigner
from app.settings import Settings

pytestmark = pytest.mark.integration


@dataclass
class UserAuthContext:
    app: FastAPI
    client: httpx.AsyncClient
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings


class IdentityFakeFlow:
    fetch_count = 0

    def __init__(self, state: str) -> None:
        self.state = state
        self.credentials = SimpleNamespace(
            scopes=list(GOOGLE_IDENTITY_SCOPES),
            granted_scopes=list(GOOGLE_IDENTITY_SCOPES),
            id_token="signed-user-identity-token",
        )

    def authorization_url(
        self,
        *,
        access_type: str,
        prompt: str,
        state: str,
        nonce: str,
    ) -> tuple[str, str]:
        assert state == self.state
        query = urlencode(
            {
                "response_type": "code",
                "client_id": "identity-route-client",
                "redirect_uri": "https://job-agent.example.test/api/v1/oauth/gmail/callback",
                "scope": " ".join(GOOGLE_IDENTITY_SCOPES),
                "state": state,
                "nonce": nonce,
                "code_challenge": "fake-pkce-challenge",
                "code_challenge_method": "S256",
                "access_type": access_type,
                "prompt": prompt,
            }
        )
        return f"https://accounts.google.com/o/oauth2/auth?{query}", state

    def fetch_token(self, *, code: str) -> None:
        assert code == "identity-route-code"
        type(self).fetch_count += 1


class GmailUserFakeFlow:
    def __init__(self) -> None:
        self.credentials = SimpleNamespace(
            refresh_token="user-gmail-refresh-token",
            scopes=list(GMAIL_DELIVERY_SCOPES),
            granted_scopes=list(GMAIL_DELIVERY_SCOPES),
        )

    def fetch_token(self, *, code: str) -> None:
        assert code == "user-gmail-code"


@pytest_asyncio.fixture
async def user_auth_context(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[UserAuthContext]:
    settings = Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        public_base_url="https://job-agent.example.test",
        secret_key="user-auth-test-secret-with-more-than-32-chars",
        token_encryption_key="user-auth-test-token-encryption-key-32chars",
        gmail_client_id="identity-route-client",
        gmail_client_secret="identity-route-secret",
        user_accounts_enabled=True,
        invite_registration_enabled=True,
        google_admin_emails=["admin@example.com"],
    )
    monkeypatch.setattr(api_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(admin_routes, "get_settings", lambda: settings)

    application = FastAPI()
    application.include_router(api_routes.router)
    application.include_router(auth_routes.router)
    application.include_router(admin_router)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sqlite_session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session

    async with sqlite_session_factory() as session:
        bootstrap = await session.get(Account, BOOTSTRAP_ADMIN_ACCOUNT_ID)
        if bootstrap is None:
            session.add(
                Account(
                    id=BOOTSTRAP_ADMIN_ACCOUNT_ID,
                    role=AccountRole.ADMIN,
                    status=AccountStatus.ACTIVE,
                    invite_allowance=0,
                    allow_open_invites=True,
                    max_profiles=100,
                    allow_phone=True,
                )
            )
            await session.commit()

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://job-agent.example.test",
        follow_redirects=False,
    ) as client:
        yield UserAuthContext(application, client, sqlite_session_factory, settings)


async def _invite(context: UserAuthContext, email: str) -> str:
    async with context.session_factory() as session:
        created = await InviteService().create(
            session,
            creator_account_id=BOOTSTRAP_ADMIN_ACCOUNT_ID,
            target_email=email,
        )
        await session.commit()
        return created.token


def _install_fake_identity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    email: str,
    nonce: str,
    subject: str,
) -> None:
    monkeypatch.setattr(
        GoogleIdentityService,
        "_flow",
        lambda self, *, state, code_verifier: IdentityFakeFlow(state),
    )

    def verify_identity(
        raw_id_token: str,
        _request: object,
        audience: str,
    ) -> dict[str, object]:
        assert raw_id_token == "signed-user-identity-token"
        assert audience == "identity-route-client"
        return {
            "sub": subject,
            "email": email,
            "email_verified": True,
            "nonce": nonce,
        }

    monkeypatch.setattr(google_auth.google_id_token, "verify_oauth2_token", verify_identity)


@pytest.mark.asyncio
async def test_invite_registration_creates_account_session_and_owned_draft_profile(
    user_auth_context: UserAuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _invite(user_auth_context, "new.user@example.test")

    accepted = await user_auth_context.client.get("/join", params={"token": token})
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/join"
    assert token not in accepted.headers["set-cookie"]

    join_page = await user_auth_context.client.get("/join")
    assert join_page.status_code == 200
    assert "ne***@example.test" in join_page.text

    started = await user_auth_context.client.get("/auth/google/register")
    assert started.status_code == 302
    query = parse_qs(urlsplit(started.headers["location"]).query)
    assert query["scope"] == [" ".join(GOOGLE_IDENTITY_SCOPES)]
    assert query["access_type"] == ["online"]
    assert query["prompt"] == ["select_account"]
    assert query["code_challenge_method"] == ["S256"]
    state = query["state"][0]
    _install_fake_identity(
        monkeypatch,
        email="New.User@Example.Test",
        nonce=query["nonce"][0],
        subject="google-user-sub-1",
    )

    callback = await user_auth_context.client.get(
        "/api/v1/oauth/gmail/callback",
        params={"code": "identity-route-code", "state": state},
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/app"
    assert user_auth_context.client.cookies.get(user_auth_context.settings.user_session_cookie_name)

    home = await user_auth_context.client.get("/app")
    assert home.status_code == 200
    assert "Мой JobHunter" in home.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', home.text)
    assert csrf is not None

    created_profile = await user_auth_context.client.post(
        "/app/profiles",
        data={
            "name": "Candidate",
            "contact_email": "candidate@example.com",
            "csrf_token": csrf.group(1),
        },
    )
    assert created_profile.status_code == 303
    assert created_profile.headers["location"] == "/app?notice=profile_created"

    async with user_auth_context.session_factory() as session:
        identity = await session.scalar(
            select(AccountIdentity).where(AccountIdentity.subject == "google-user-sub-1")
        )
        assert identity is not None
        assert identity.email == "new.user@example.test"
        account = await session.get(Account, identity.account_id)
        assert account is not None
        assert account.invite_allowance == 0
        profile = await session.scalar(
            select(UserProfile).where(UserProfile.owner_account_id == account.id)
        )
        assert profile is not None
        assert profile.name == "Candidate"
        assert profile.status == ProfileStatus.DRAFT
        invite = await session.scalar(
            select(Invite).where(Invite.redeemed_by_account_id == account.id)
        )
        assert invite is not None
        assert invite.redeemed_at is not None


@pytest.mark.asyncio
async def test_bound_invite_rejects_different_google_email(
    user_auth_context: UserAuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _invite(user_auth_context, "expected@example.test")
    assert (await user_auth_context.client.get("/join", params={"token": token})).status_code == 303
    started = await user_auth_context.client.get("/auth/google/register")
    query = parse_qs(urlsplit(started.headers["location"]).query)
    _install_fake_identity(
        monkeypatch,
        email="other@example.test",
        nonce=query["nonce"][0],
        subject="wrong-email-sub",
    )
    callback = await user_auth_context.client.get(
        "/api/v1/oauth/gmail/callback",
        params={"code": "identity-route-code", "state": query["state"][0]},
    )
    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/join?error=")
    assert (
        user_auth_context.client.cookies.get(user_auth_context.settings.user_session_cookie_name)
        is None
    )

    async with user_auth_context.session_factory() as session:
        invite = await session.scalar(
            select(Invite).where(Invite.target_email == "expected@example.test")
        )
        assert invite is not None
        assert invite.redeemed_at is None


@pytest.mark.asyncio
async def test_google_login_requires_previously_registered_subject(
    user_auth_context: UserAuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await user_auth_context.client.get("/auth/google/login")
    query = parse_qs(urlsplit(started.headers["location"]).query)
    _install_fake_identity(
        monkeypatch,
        email="nobody@example.test",
        nonce=query["nonce"][0],
        subject="not-registered-sub",
    )
    callback = await user_auth_context.client.get(
        "/api/v1/oauth/gmail/callback",
        params={"code": "identity-route-code", "state": query["state"][0]},
    )
    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/login?error=")


@pytest.mark.asyncio
async def test_user_auth_session_is_invalidated_by_account_session_version(
    user_auth_context: UserAuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _invite(user_auth_context, "versioned@example.test")
    await user_auth_context.client.get("/join", params={"token": token})
    started = await user_auth_context.client.get("/auth/google/register")
    query = parse_qs(urlsplit(started.headers["location"]).query)
    _install_fake_identity(
        monkeypatch,
        email="versioned@example.test",
        nonce=query["nonce"][0],
        subject="versioned-sub",
    )
    await user_auth_context.client.get(
        "/api/v1/oauth/gmail/callback",
        params={"code": "identity-route-code", "state": query["state"][0]},
    )
    assert (await user_auth_context.client.get("/app")).status_code == 200

    async with user_auth_context.session_factory() as session:
        identity = await session.scalar(
            select(AccountIdentity).where(AccountIdentity.subject == "versioned-sub")
        )
        assert identity is not None
        account = await session.get(Account, identity.account_id)
        assert account is not None
        account.session_version += 1
        await session.commit()

    expired = await user_auth_context.client.get("/app")
    assert expired.status_code == 303
    assert expired.headers["location"] == "/login"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_invite_registration_browser_roundtrip_three_clean_contexts(
    user_auth_context: UserAuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from playwright.async_api import async_playwright

    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = int(port_socket.getsockname()[1])
    base_url = f"http://localhost:{port}"

    settings = user_auth_context.settings.model_copy(update={"public_base_url": base_url})
    monkeypatch.setattr(api_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)

    server = uvicorn.Server(
        uvicorn.Config(user_auth_context.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    try:
        async with httpx.AsyncClient(base_url=base_url) as readiness_client:
            for _ in range(100):
                try:
                    response = await readiness_client.get("/login")
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("local user-auth browser server did not start")

        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(headless=True)
            try:
                for index in range(3):
                    email = f"browser-{index}@example.test"
                    subject = f"browser-sub-{index}"
                    token = await _invite(user_auth_context, email)
                    context = await browser.new_context()
                    page = await context.new_page()
                    join = await page.goto(
                        f"{base_url}/join?{urlencode({'token': token})}",
                        wait_until="domcontentloaded",
                    )
                    assert join is not None
                    assert join.status == 200
                    assert page.url == f"{base_url}/join"
                    assert token not in page.url

                    started = await context.request.get(
                        f"{base_url}/auth/google/register",
                        max_redirects=0,
                    )
                    assert started.status == 302
                    query = parse_qs(urlsplit(started.headers["location"]).query)
                    assert query["scope"] == [" ".join(GOOGLE_IDENTITY_SCOPES)]
                    assert query["code_challenge_method"] == ["S256"]
                    _install_fake_identity(
                        monkeypatch,
                        email=email,
                        nonce=query["nonce"][0],
                        subject=subject,
                    )
                    callback = await page.goto(
                        f"{base_url}/api/v1/oauth/gmail/callback?"
                        + urlencode(
                            {
                                "code": "identity-route-code",
                                "state": query["state"][0],
                            }
                        ),
                        wait_until="domcontentloaded",
                    )
                    assert callback is not None
                    assert callback.status == 200
                    assert page.url == f"{base_url}/app"
                    assert "Мой JobHunter" in await page.content()
                    await context.close()
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_admin_can_manage_registered_account_lifecycle(
    user_auth_context: UserAuthContext,
) -> None:
    async with user_auth_context.session_factory() as session:
        account = Account(
            role=AccountRole.USER,
            status=AccountStatus.ACTIVE,
            invite_allowance=0,
            max_profiles=1,
        )
        session.add(account)
        await session.flush()
        session.add(
            AccountIdentity(
                account_id=account.id,
                provider="google",
                subject="managed-subject",
                email="managed@example.com",
                email_verified=True,
            )
        )
        profile = UserProfile(
            owner_account_id=account.id,
            status=ProfileStatus.DRAFT,
            name="Managed candidate",
        )
        session.add(profile)
        await session.flush()
        session.add(
            JobPreference(
                profile_id=profile.id,
                auto_send_enabled=True,
                global_pause=False,
            )
        )
        await session.commit()
        account_id = account.id
        profile_id = profile.id

    admin_session = SessionSigner(user_auth_context.settings.secret_key.get_secret_value()).issue(
        user_auth_context.settings.admin_username
    )
    user_auth_context.client.cookies.set(
        user_auth_context.settings.session_cookie_name,
        admin_session,
    )

    page = await user_auth_context.client.get("/admin/accounts")
    assert page.status_code == 200
    assert "managed@example.com" in page.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None
    csrf_token = csrf.group(1)

    limits = await user_auth_context.client.post(
        f"/admin/accounts/{account_id}/limits",
        data={
            "invite_allowance": "3",
            "max_profiles": "2",
            "csrf_token": csrf_token,
        },
    )
    assert limits.status_code == 303

    activated = await user_auth_context.client.post(
        f"/admin/profiles/{profile_id}/status",
        data={"profile_status": "active", "csrf_token": csrf_token},
    )
    assert activated.status_code == 303

    suspended = await user_auth_context.client.post(
        f"/admin/accounts/{account_id}/suspend",
        data={"csrf_token": csrf_token},
    )
    assert suspended.status_code == 303

    async with user_auth_context.session_factory() as session:
        stored_account = await session.get(Account, account_id)
        stored_profile = await session.get(UserProfile, profile_id)
        preference = await session.scalar(
            select(JobPreference).where(JobPreference.profile_id == profile_id)
        )
        assert stored_account is not None
        assert stored_account.status == AccountStatus.SUSPENDED
        assert stored_account.session_version == 1
        assert stored_account.invite_allowance == 3
        assert stored_account.max_profiles == 2
        assert stored_profile is not None
        assert stored_profile.status == ProfileStatus.ACTIVE
        assert preference is not None
        assert preference.global_pause is True

    reactivated = await user_auth_context.client.post(
        f"/admin/accounts/{account_id}/reactivate",
        data={"csrf_token": csrf_token},
    )
    assert reactivated.status_code == 303

    async with user_auth_context.session_factory() as session:
        stored_account = await session.get(Account, account_id)
        preference = await session.scalar(
            select(JobPreference).where(JobPreference.profile_id == profile_id)
        )
        assert stored_account is not None
        assert stored_account.status == AccountStatus.ACTIVE
        assert preference is not None
        assert preference.global_pause is True


@pytest.mark.asyncio
async def test_user_gmail_oauth_is_bound_to_logged_in_account(
    user_auth_context: UserAuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with user_auth_context.session_factory() as session:
        account = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        session.add(account)
        await session.flush()
        session.add(
            AccountIdentity(
                account_id=account.id,
                provider="google",
                subject="gmail-owner-sub",
                email="gmail.owner@example.com",
                email_verified=True,
            )
        )
        await session.commit()
        account_id = account.id
        session_version = account.session_version

    user_session = AccountSessionSigner(
        user_auth_context.settings.secret_key.get_secret_value()
    ).issue(account_id, session_version)
    user_auth_context.client.cookies.set(
        user_auth_context.settings.user_session_cookie_name,
        user_session,
    )

    started = await user_auth_context.client.get("/app/gmail/connect")
    assert started.status_code == 302
    query = parse_qs(urlsplit(started.headers["location"]).query)
    assert set(query["scope"][0].split()) == set(GMAIL_DELIVERY_SCOPES)
    state = query["state"][0]

    monkeypatch.setattr(
        GmailOAuthService,
        "_flow",
        lambda self, state=None, code_verifier=None, scopes=None: GmailUserFakeFlow(),
    )
    callback = await user_auth_context.client.get(
        "/api/v1/oauth/gmail/callback",
        params={"code": "user-gmail-code", "state": state},
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/app?notice=gmail_connected"

    async with user_auth_context.session_factory() as session:
        credential = await session.scalar(
            select(OAuthCredential).where(OAuthCredential.account_id == account_id)
        )
        assert credential is not None
        assert credential.provider == "gmail"

    home = await user_auth_context.client.get("/app")
    assert home.status_code == 200
    assert "Подключён и готов к работе" in home.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', home.text)
    assert csrf is not None

    disconnected = await user_auth_context.client.post(
        "/app/gmail/disconnect",
        data={"csrf_token": csrf.group(1)},
    )
    assert disconnected.status_code == 303
    assert disconnected.headers["location"] == "/app?notice=gmail_disconnected"

    async with user_auth_context.session_factory() as session:
        credential = await session.scalar(
            select(OAuthCredential).where(OAuthCredential.account_id == account_id)
        )
        assert credential is None


@pytest.mark.asyncio
async def test_admin_invites_are_embedded_in_users_page(
    user_auth_context: UserAuthContext,
) -> None:
    admin_session = SessionSigner(user_auth_context.settings.secret_key.get_secret_value()).issue(
        user_auth_context.settings.admin_username
    )
    user_auth_context.client.cookies.set(
        user_auth_context.settings.session_cookie_name,
        admin_session,
    )

    page = await user_auth_context.client.get("/admin/accounts")
    assert page.status_code == 200
    assert "Пользователи" in page.text
    assert "Приглашения" in page.text
    assert 'action="/admin/invites"' in page.text
    assert 'href="/admin/invites"' not in page.text

    legacy_get = await user_auth_context.client.get("/admin/invites")
    assert legacy_get.status_code == 303
    assert legacy_get.headers["location"] == "/admin/accounts"

    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None
    created = await user_auth_context.client.post(
        "/admin/invites",
        data={
            "target_email": "embedded.invite@example.com",
            "ttl_days": "14",
            "csrf_token": csrf.group(1),
        },
    )
    assert created.status_code == 200
    assert "Пользователи" in created.text
    assert "Ссылка создана. Она показывается только сейчас." in created.text
    assert "/join?token=jhi_" in created.text
    assert "embedded.invite@example.com" in created.text

    async with user_auth_context.session_factory() as session:
        invite = await session.scalar(
            select(Invite).where(Invite.target_email == "embedded.invite@example.com")
        )
        assert invite is not None
        invite_id = invite.id

    csrf_after = re.search(r'name="csrf_token" value="([^"]+)"', created.text)
    assert csrf_after is not None
    revoked = await user_auth_context.client.post(
        f"/admin/invites/{invite_id}/revoke",
        data={"csrf_token": csrf_after.group(1)},
    )
    assert revoked.status_code == 303
    assert revoked.headers["location"] == "/admin/accounts"


@pytest.mark.asyncio
async def test_login_is_single_entry_page_for_users_and_admins(
    user_auth_context: UserAuthContext,
) -> None:
    page = await user_auth_context.client.get("/login")
    assert page.status_code == 200
    assert 'class="login-card"' in page.text
    assert 'href="/auth/google/login"' in page.text
    assert 'class="login-divider"' not in page.text

    legacy_admin = await user_auth_context.client.get("/admin/login")
    assert legacy_admin.status_code == 303
    assert legacy_admin.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_unified_google_login_routes_admin_without_user_identity(
    user_auth_context: UserAuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with user_auth_context.session_factory() as session:
        identity = await session.scalar(
            select(AccountIdentity).where(AccountIdentity.email == "admin@example.com")
        )
        assert identity is None

    started = await user_auth_context.client.get("/auth/google/login")
    assert started.status_code == 302
    query = parse_qs(urlsplit(started.headers["location"]).query)
    _install_fake_identity(
        monkeypatch,
        email="admin@example.com",
        nonce=query["nonce"][0],
        subject="google-admin-sub",
    )

    callback = await user_auth_context.client.get(
        "/api/v1/oauth/gmail/callback",
        params={"code": "identity-route-code", "state": query["state"][0]},
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/admin"
    assert user_auth_context.client.cookies.get(user_auth_context.settings.session_cookie_name)
    assert (
        user_auth_context.client.cookies.get(user_auth_context.settings.user_session_cookie_name)
        is None
    )

    async with user_auth_context.session_factory() as session:
        identity = await session.scalar(
            select(AccountIdentity).where(AccountIdentity.subject == "google-admin-sub")
        )
        assert identity is None


@pytest.mark.asyncio
async def test_public_root_dispatches_by_session(
    user_auth_context: UserAuthContext,
) -> None:
    guest = await user_auth_context.client.get("/")
    assert guest.status_code == 303
    assert guest.headers["location"] == "/login"

    async with user_auth_context.session_factory() as session:
        account = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        session.add(account)
        await session.commit()
        account_id = account.id
        session_version = account.session_version

    user_token = AccountSessionSigner(
        user_auth_context.settings.secret_key.get_secret_value()
    ).issue(account_id, session_version)
    user_auth_context.client.cookies.set(
        user_auth_context.settings.user_session_cookie_name,
        user_token,
    )
    user_root = await user_auth_context.client.get("/")
    assert user_root.status_code == 303
    assert user_root.headers["location"] == "/app"

    user_auth_context.client.cookies.delete(user_auth_context.settings.user_session_cookie_name)
    admin_token = SessionSigner(user_auth_context.settings.secret_key.get_secret_value()).issue(
        user_auth_context.settings.admin_username
    )
    user_auth_context.client.cookies.set(
        user_auth_context.settings.session_cookie_name,
        admin_token,
    )
    admin_root = await user_auth_context.client.get("/")
    assert admin_root.status_code == 303
    assert admin_root.headers["location"] == "/admin"
