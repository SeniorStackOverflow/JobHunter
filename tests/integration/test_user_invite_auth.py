from __future__ import annotations

import asyncio
import re
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

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
from app.auth import access as auth_access
from app.auth import google as google_auth
from app.auth import routes as auth_routes
from app.auth.google import GOOGLE_IDENTITY_SCOPES, GoogleIdentityService
from app.database.session import get_session
from app.email.oauth import GMAIL_DELIVERY_SCOPES, GmailOAuthService
from app.models.constants import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.models.entities import (
    Account,
    AccountIdentity,
    Alert,
    Application,
    CanonicalJob,
    EmployerContact,
    Invite,
    JobPreference,
    JobSource,
    OAuthCredential,
    ProfileSourcePreference,
    Resume,
    ScanRun,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    AccountRole,
    AccountStatus,
    ApplicationStatus,
    ContactType,
    ProfileStatus,
    RunStatus,
    ScanType,
)
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
    tmp_path: Path,
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
        resume_storage_path=tmp_path / "resumes",
    )
    monkeypatch.setattr(api_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_access, "get_settings", lambda: settings)
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
    assert created_profile.headers["location"].startswith("/app?view=settings&profile_id=")
    assert created_profile.headers["location"].endswith("&notice=profile_created")

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


@pytest.mark.asyncio
async def test_user_workspace_routes_keep_profile_settings_and_sources_owned(
    user_auth_context: UserAuthContext,
) -> None:
    async with user_auth_context.session_factory() as session:
        first_owner = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        second_owner = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        session.add_all([first_owner, second_owner])
        await session.flush()
        first_profile = UserProfile(name="First", owner_account_id=first_owner.id)
        second_profile = UserProfile(name="Second", owner_account_id=second_owner.id)
        source = JobSource(
            name="Shared source",
            base_url="https://jobs.example.test",
            adapter_type="fixture_source",
        )
        session.add_all([first_profile, second_profile, source])
        await session.commit()
        first_id, second_id, source_id = first_profile.id, second_profile.id, source.id

    signed = AccountSessionSigner(user_auth_context.settings.secret_key.get_secret_value()).issue(
        first_owner.id, first_owner.session_version
    )
    user_auth_context.client.cookies.set(
        user_auth_context.settings.user_session_cookie_name, signed
    )
    home = await user_auth_context.client.get("/app", params={"view": "settings"})
    assert home.status_code == 200
    assert "First" in home.text
    assert "Second" not in home.text
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', home.text)
    assert csrf_match is not None
    csrf_token = csrf_match.group(1)

    foreign_view = await user_auth_context.client.get("/app", params={"profile_id": second_id})
    assert foreign_view.status_code == 404
    foreign_updates = [
        (f"/app/profiles/{second_id}/update", {"name": "Changed"}),
        (f"/app/profiles/{second_id}/preferences", {"maximum_daily_applications": "1"}),
        (f"/app/profiles/{second_id}/sources/{source_id}", {"enabled": "false"}),
        (f"/app/profiles/{second_id}/activate", {}),
    ]
    for path, fields in foreign_updates:
        response = await user_auth_context.client.post(
            path, data={"csrf_token": csrf_token, **fields}
        )
        assert response.status_code == 404, path

    own_selection = await user_auth_context.client.post(
        f"/app/profiles/{first_id}/sources/{source_id}",
        data={"csrf_token": csrf_token, "enabled": "false"},
    )
    assert own_selection.status_code == 303
    async with user_auth_context.session_factory() as session:
        selected = await session.get(ProfileSourcePreference, (first_id, source_id))
        untouched = await session.get(ProfileSourcePreference, (second_id, source_id))
        other_profile = await session.get(UserProfile, second_id)
        assert selected is not None and selected.enabled is False
        assert untouched is None
        assert other_profile is not None and other_profile.name == "Second"


@pytest.mark.asyncio
async def test_admin_source_selection_is_profile_scoped_and_keeps_crawler_enabled(
    user_auth_context: UserAuthContext,
) -> None:
    async with user_auth_context.session_factory() as session:
        first_profile = UserProfile(
            name="Admin profile", owner_account_id=BOOTSTRAP_ADMIN_ACCOUNT_ID
        )
        second_profile = UserProfile(
            name="Other profile", owner_account_id=BOOTSTRAP_ADMIN_ACCOUNT_ID
        )
        source = JobSource(
            name="Shared source",
            base_url="https://jobs.example.test",
            adapter_type="fixture_source",
        )
        session.add_all([first_profile, second_profile, source])
        await session.flush()
        session.add(
            ScanRun(
                source_id=source.id,
                scan_type=ScanType.INCREMENTAL,
                status=RunStatus.SUCCEEDED,
                started_at=datetime.now(UTC),
                new_jobs=7,
            )
        )
        await session.commit()
        first_id, second_id, source_id = first_profile.id, second_profile.id, source.id

    signed = SessionSigner(user_auth_context.settings.secret_key.get_secret_value()).issue(
        user_auth_context.settings.admin_username
    )
    user_auth_context.client.cookies.set(user_auth_context.settings.session_cookie_name, signed)
    settings_page = await user_auth_context.client.get(
        "/admin", params={"view": "settings", "profile_id": first_id}
    )
    assert settings_page.status_code == 200
    assert "Источники для Admin profile" in settings_page.text
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', settings_page.text)
    assert csrf_match is not None
    response = await user_auth_context.client.post(
        f"/admin/profile-sources/{source_id}/selection",
        data={"profile_id": str(first_id), "enabled": "false", "csrf_token": csrf_match.group(1)},
    )
    assert response.status_code == 303
    async with user_auth_context.session_factory() as session:
        first_choice = await session.get(ProfileSourcePreference, (first_id, source_id))
        second_choice = await session.get(ProfileSourcePreference, (second_id, source_id))
        shared_source = await session.get(JobSource, source_id)
        assert first_choice is not None and first_choice.enabled is False
        assert second_choice is None
        assert shared_source is not None and shared_source.enabled is True
    first_overview = await user_auth_context.client.get(
        "/admin", params={"view": "overview", "profile_id": first_id}
    )
    second_overview = await user_auth_context.client.get(
        "/admin", params={"view": "overview", "profile_id": second_id}
    )
    assert first_overview.status_code == second_overview.status_code == 200
    assert 'Новых вакансий сегодня</div><div class="metric-value">0</div>' in first_overview.text
    assert 'Новых вакансий сегодня</div><div class="metric-value">7</div>' in second_overview.text
    assert 'class="notification-count">' not in first_overview.text
    assert 'class="notification-count">1</span>' in second_overview.text


@pytest.mark.asyncio
async def test_user_can_upload_review_and_delete_own_resume(
    user_auth_context: UserAuthContext,
) -> None:
    async with user_auth_context.session_factory() as session:
        account = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        session.add(account)
        await session.flush()
        profile = UserProfile(name="Candidate", owner_account_id=account.id)
        session.add(profile)
        await session.commit()
        profile_id = profile.id
    token = AccountSessionSigner(user_auth_context.settings.secret_key.get_secret_value()).issue(
        account.id, account.session_version
    )
    user_auth_context.client.cookies.set(user_auth_context.settings.user_session_cookie_name, token)
    settings_page = await user_auth_context.client.get("/app", params={"view": "settings"})
    assert settings_page.status_code == 200
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', settings_page.text)
    assert csrf_match is not None
    csrf_token = csrf_match.group(1)

    pdf = b"%PDF-1.7\nuser resume\n%%EOF"
    uploaded = await user_auth_context.client.post(
        f"/app/profiles/{profile_id}/resumes",
        data={
            "name": "CV",
            "category": "office",
            "make_default": "true",
            "csrf_token": csrf_token,
        },
        files={"file": ("cv.pdf", pdf, "application/pdf")},
    )
    assert uploaded.status_code == 303
    async with user_auth_context.session_factory() as session:
        resume = await session.scalar(select(Resume).where(Resume.profile_id == profile_id))
        assert resume is not None
        assert resume.is_default is True
        resume_id = resume.id
    viewed = await user_auth_context.client.get(f"/app/resumes/{resume_id}/file")
    assert viewed.status_code == 200
    assert viewed.content == pdf
    verified = await user_auth_context.client.post(
        f"/app/resumes/{resume_id}/verify", data={"csrf_token": csrf_token}
    )
    assert verified.status_code == 303
    settings_page = await user_auth_context.client.get(
        "/app", params={"view": "settings", "profile_id": profile_id}
    )
    assert settings_page.status_code == 200
    assert f"/app/resumes/{resume_id}/delete" in settings_page.text
    deleted = await user_auth_context.client.post(
        f"/app/resumes/{resume_id}/delete", data={"csrf_token": csrf_token}
    )
    assert deleted.status_code == 303
    async with user_auth_context.session_factory() as session:
        assert await session.get(Resume, resume_id) is None


@pytest.mark.asyncio
async def test_user_history_and_decisions_paginate_all_owned_applications(
    user_auth_context: UserAuthContext,
) -> None:
    async with user_auth_context.session_factory() as session:
        account = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        session.add(account)
        await session.flush()
        profile = UserProfile(name="Candidate", owner_account_id=account.id)
        source = JobSource(
            name="Jobs", base_url="https://jobs.example.test", adapter_type="fixture_source"
        )
        session.add_all([profile, source])
        await session.flush()
        resume = Resume(
            profile_id=profile.id,
            name="CV",
            category="office",
            storage_key="fixture/cv.pdf",
            original_filename="cv.pdf",
            mime_type="application/pdf",
            sha256="a" * 64,
        )
        session.add(resume)
        await session.flush()
        canonical_jobs = [
            CanonicalJob(
                normalized_company="company",
                normalized_title=f"role {index}",
                canonical_fingerprint=uuid4().hex,
            )
            for index in range(26)
        ]
        session.add_all(canonical_jobs)
        await session.flush()
        jobs = [
            SourceJob(
                source_id=source.id,
                canonical_job_id=canonical.id,
                external_job_id=str(index),
                canonical_url=f"https://jobs.example.test/{index}",
                title=f"Role {index}",
                content_hash="a" * 64,
                matching_content_hash="b" * 64,
                source_fingerprint="c" * 64,
            )
            for index, canonical in enumerate(canonical_jobs)
        ]
        session.add_all(jobs)
        await session.flush()
        contacts = [
            EmployerContact(
                canonical_job_id=canonical.id,
                source_job_id=job.id,
                value=f"hr{index}@example.test",
                contact_type=ContactType.EMAIL,
                discovery_source="fixture",
                evidence_url=job.canonical_url,
            )
            for index, (canonical, job) in enumerate(zip(canonical_jobs, jobs, strict=True))
        ]
        session.add_all(contacts)
        await session.flush()
        session.add_all(
            [
                Application(
                    profile_id=profile.id,
                    canonical_job_id=canonical.id,
                    source_job_id=job.id,
                    resume_id=resume.id,
                    recipient_contact_id=contact.id,
                    subject=f"Application {index}",
                    body="Letter",
                    language="en",
                    status=ApplicationStatus.PENDING_REVIEW,
                    idempotency_key=uuid4().hex,
                )
                for index, (canonical, job, contact) in enumerate(
                    zip(canonical_jobs, jobs, contacts, strict=True)
                )
            ]
        )
        await session.commit()
        profile_id = profile.id

    token = AccountSessionSigner(user_auth_context.settings.secret_key.get_secret_value()).issue(
        account.id, account.session_version
    )
    user_auth_context.client.cookies.set(user_auth_context.settings.user_session_cookie_name, token)
    for view, row_class in (("history", "history-row"), ("decisions", "compact-row")):
        first = await user_auth_context.client.get(
            "/app", params={"view": view, "profile_id": profile_id}
        )
        assert first.status_code == 200
        assert "Страница 1 из 2" in first.text
        assert first.text.count(f'class="{row_class}"') == 25
        second = await user_auth_context.client.get(
            "/app", params={"view": view, "profile_id": profile_id, "page": 2}
        )
        assert second.status_code == 200
        assert "Страница 2 из 2" in second.text
        assert second.text.count(f'class="{row_class}"') == 1


async def _assert_notification_popover_visible(page: object, *, width: int, height: int) -> int:
    from playwright.async_api import Page

    assert isinstance(page, Page)
    await page.set_viewport_size({"width": width, "height": height})
    notifications = page.locator("[data-notifications]")
    if await notifications.evaluate("element => element.open"):
        await notifications.locator("summary").click()
    await notifications.locator("summary").click()
    geometry = await notifications.evaluate(
        """element => {
          const popover = element.querySelector('.notification-popover');
          const badge = element.querySelector('.notification-count');
          const rect = popover.getBoundingClientRect();
          const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + 35);
          const badgeRect = badge?.getBoundingClientRect();
          return {
            open: element.open,
            hitPopover: popover.contains(hit),
            overflow: getComputedStyle(element).overflow,
            borderWidth: getComputedStyle(element).borderTopWidth,
            count: badge?.textContent.trim() ?? null,
            headCount: popover.querySelector('.notification-head .badge').textContent.trim(),
            itemCount: popover.querySelectorAll('.notification-item').length,
            badgeInViewport: !badgeRect || (
              badgeRect.left >= 0 && badgeRect.top >= 0 &&
              badgeRect.right <= innerWidth && badgeRect.bottom <= innerHeight
            ),
          };
        }"""
    )
    assert geometry["open"] is True
    assert geometry["hitPopover"] is True
    assert geometry["overflow"] == "visible"
    assert geometry["borderWidth"] == "0px"
    assert geometry["badgeInViewport"] is True
    assert geometry["count"] == (geometry["headCount"] if geometry["headCount"] != "0" else None)
    return geometry["itemCount"]


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
    monkeypatch.setattr(auth_access, "get_settings", lambda: settings)

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

        async with user_auth_context.session_factory() as session:
            session.add(
                JobSource(
                    name="Browser fixture jobs",
                    base_url="https://jobs.example.test",
                    adapter_type="fixture_source",
                )
            )
            await session.commit()

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
                    assert (
                        await _assert_notification_popover_visible(page, width=1365, height=768) > 0
                    )
                    assert (
                        await _assert_notification_popover_visible(page, width=390, height=844) > 0
                    )
                    await page.goto(f"{base_url}/app?view=settings")
                    await page.locator("form[action='/app/profiles'] input[name='name']").fill(
                        f"Browser candidate {index}"
                    )
                    await page.locator("form[action='/app/profiles'] button").click()
                    assert "notice=profile_created" in page.url
                    source_form = page.locator("form[action*='/sources/']")
                    assert await source_form.count() == 1
                    await source_form.locator("button").click()
                    assert (
                        "Исключён"
                        in await page.locator("form[action*='/sources/']")
                        .locator("xpath=..")
                        .inner_text()
                    )
                    await page.locator("form[action*='/sources/'] button").click()
                    assert (
                        "В моём поиске"  # noqa: RUF001
                        in await page.locator("form[action*='/sources/']")
                        .locator("xpath=..")
                        .inner_text()
                    )
                    await context.close()
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await server_task


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_admin_notification_acknowledgement_three_clean_browser_contexts(
    user_auth_context: UserAuthContext,
) -> None:
    async with user_auth_context.session_factory() as session:
        profile = UserProfile(name="Admin", owner_account_id=BOOTSTRAP_ADMIN_ACCOUNT_ID)
        session.add(profile)
        await session.commit()
        profile_id = profile.id

    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = int(port_socket.getsockname()[1])
    base_url = f"http://localhost:{port}"
    signed = SessionSigner(user_auth_context.settings.secret_key.get_secret_value()).issue(
        user_auth_context.settings.admin_username
    )
    server = uvicorn.Server(
        uvicorn.Config(user_auth_context.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    try:
        async with httpx.AsyncClient(base_url=base_url) as readiness_client:
            for _ in range(100):
                try:
                    if (await readiness_client.get("/login")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("local notification browser server did not start")

        from playwright.async_api import async_playwright

        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(headless=True)
            try:
                for index in range(3):
                    async with user_auth_context.session_factory() as session:
                        alert = Alert(
                            severity="error",
                            code=f"browser_alert_{index}",
                            message=f"Browser alert {index}",
                        )
                        session.add(alert)
                        await session.commit()
                        alert_id = alert.id
                    context = await browser.new_context()
                    await context.add_cookies(
                        [
                            {
                                "name": user_auth_context.settings.session_cookie_name,
                                "value": signed,
                                "url": base_url,
                            }
                        ]
                    )
                    page = await context.new_page()
                    response = await page.goto(
                        f"{base_url}/admin?view=overview&profile_id={profile_id}",
                        wait_until="domcontentloaded",
                    )
                    assert response is not None and response.status == 200
                    assert (
                        await _assert_notification_popover_visible(page, width=1365, height=768) > 0
                    )
                    dashboard_count = await page.locator(".notification-head .badge").text_content()
                    assert (
                        await _assert_notification_popover_visible(page, width=390, height=844) > 0
                    )
                    await page.goto(f"{base_url}/admin/accounts")
                    assert await page.locator(".app-shell .sidebar").is_visible()
                    title = await page.locator(".topbar .topbar-title strong").inner_text()
                    assert title == "Пользователи"
                    active_link = await page.locator(".side-nav .nav-link.is-active").inner_text()
                    assert active_link == "Пользователи"
                    icons = await page.locator(".side-nav .nav-svg").evaluate_all(
                        "elements => elements.map(element => { "
                        "const r = element.getBoundingClientRect(); "
                        "return [r.width, r.height]; })"
                    )
                    assert icons == [[20, 20]] * 6
                    assert (
                        await page.locator(".notification-head .badge").text_content()
                        == dashboard_count
                    )
                    assert (
                        await _assert_notification_popover_visible(page, width=1365, height=768) > 0
                    )
                    assert (
                        await _assert_notification_popover_visible(page, width=390, height=844) > 0
                    )
                    assert await page.get_by_text(f"Browser alert {index}").is_visible()
                    await page.locator(
                        f"form[action='/admin/alerts/{alert_id}/acknowledge'] button"
                    ).click()
                    assert page.url == f"{base_url}/admin/accounts"
                    assert (
                        await _assert_notification_popover_visible(page, width=1365, height=768)
                        == 0
                    )
                    async with user_auth_context.session_factory() as session:
                        stored = await session.get(Alert, alert_id)
                        assert stored is not None and stored.acknowledged is True
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

    user_settings = await user_auth_context.client.get("/app?view=settings")
    assert user_settings.status_code == 200
    assert "gmail.owner@example.com" in user_settings.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', user_settings.text)
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
