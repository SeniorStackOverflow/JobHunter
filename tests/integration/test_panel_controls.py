from __future__ import annotations

# Russian labels are assertions against the rendered interface.
# ruff: noqa: RUF001
import asyncio
import base64
import hashlib
import html
import json
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from google.auth import crypt, jwt
from google_auth_oauthlib.flow import Flow
from pydantic import SecretStr
from sqlalchemy import select

from app.admin import invite_routes
from app.email import oauth
from app.email.oauth import GOOGLE_ADMIN_SCOPES, GmailOAuthService
from app.models.entities import Account, Application, EmailDelivery, JobPreference, UserProfile
from app.models.enums import AccountStatus, ApplicationStatus, DeliveryStatus
from app.security.auth import AccountSessionSigner, SessionSigner, hash_password
from tests.integration.test_interfaces import _seed_review_application
from tests.integration.test_user_invite_auth import UserAuthContext
from tests.integration.test_user_invite_auth import user_auth_context as user_auth_context

pytestmark = pytest.mark.integration
PASSWORD = "panel-local-test-password"


@dataclass
class PanelScenario:
    context: UserAuthContext
    primary_id: UUID
    secondary_id: UUID
    user_id: UUID
    user_version: int


@pytest_asyncio.fixture
async def panel_scenario(
    user_auth_context: UserAuthContext, monkeypatch: pytest.MonkeyPatch
) -> PanelScenario:
    context = user_auth_context
    monkeypatch.setattr(invite_routes, "get_settings", lambda: context.settings)
    context.settings.admin_password_hash = SecretStr(hash_password(PASSWORD))
    primary = await _seed_review_application(
        context.session_factory,
        context.settings,
        suffix="panel-primary",
        status=ApplicationStatus.SENT,
    )
    secondary = await _seed_review_application(
        context.session_factory, context.settings, suffix="panel-secondary"
    )
    bounced = await _seed_review_application(
        context.session_factory,
        context.settings,
        suffix="panel-bounce",
        status=ApplicationStatus.SENT,
    )
    async with context.session_factory() as session:
        account = Account(status=AccountStatus.ACTIVE)
        session.add(account)
        await session.flush()
        for profile_id, name, default in (
            (primary["profile_id"], "Первый профиль", True),
            (secondary["profile_id"], "Второй профиль", False),
            (bounced["profile_id"], "Архивный профиль", False),
        ):
            profile = await session.get(UserProfile, profile_id)
            assert profile is not None
            profile.name, profile.is_default = name, default
            preference = await session.scalar(
                select(JobPreference).where(JobPreference.profile_id == profile.id)
            )
            assert preference is not None
            preference.auto_send_enabled = False
            preference.maximum_daily_applications = 20 if default else 10
            if profile.id == secondary["profile_id"]:
                profile.owner_account_id = account.id
        first_application = await session.get(Application, primary["application_id"])
        bounced_application = await session.get(Application, bounced["application_id"])
        assert first_application is not None and bounced_application is not None
        first_application.sent_at = datetime.now(UTC)
        bounced_application.profile_id = primary["profile_id"]
        bounced_application.sent_at = datetime.now(UTC)
        session.add(
            EmailDelivery(
                application_id=bounced_application.id,
                status=DeliveryStatus.BOUNCED_PERMANENT,
                rfc_message_id="panel-bounce@example.test",
                recipient="jobs@example.test",
                provider="fake",
            )
        )
        await session.commit()
        return PanelScenario(
            context,
            primary["profile_id"],
            secondary["profile_id"],
            account.id,
            account.session_version,
        )


async def test_panel_controls_preserve_scope_and_gmail_requires_admin(
    panel_scenario: PanelScenario,
) -> None:
    scenario = panel_scenario
    context = scenario.context
    legacy = await context.client.get("/admin/auth/google?consent=1")
    assert legacy.headers["location"] == "/admin/oauth/gmail/connect"
    denied = await context.client.get(legacy.headers["location"])
    assert denied.status_code == 303 and denied.headers["location"] == "/login"
    ordinary_login = await context.client.get("/admin/auth/google")
    assert ordinary_login.headers["location"] == "/auth/google/login"
    context.client.cookies.set(
        context.settings.session_cookie_name,
        SessionSigner(context.settings.secret_key.get_secret_value()).issue(
            context.settings.admin_username
        ),
    )
    for profile_id, expected in (
        (scenario.primary_id, "1 из 20"),
        (scenario.secondary_id, "0 из 10"),
    ):
        page = await context.client.get("/admin/accounts", params={"profile_id": str(profile_id)})
        assert page.status_code == 200
        assert f'aria-label="Отправлено сегодня {expected}"' in page.text
        assert "sidebar-status-row" in page.text
        assert 'aria-label="Управление доступом"' in page.text
    invalid = await context.client.get("/admin/accounts", params={"profile_id": str(uuid4())})
    assert invalid.status_code == 404
    assert (
        await context.client.post("/admin/pause/false", data={"csrf_token": "wrong"})
    ).status_code == 403


@pytest.mark.e2e
async def test_panel_and_gmail_browser_in_three_clean_contexts(
    panel_scenario: PanelScenario, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from playwright.async_api import async_playwright, expect

    scenario = panel_scenario
    context = scenario.context
    settings = context.settings
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    base_url = f"http://localhost:{port}"
    settings.public_base_url = base_url
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = crypt.RSASigner.from_string(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_key = (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    grants: dict[str, dict[str, str]] = {}
    token_exchanges = 0

    class LocalCertificates:
        def __call__(self, url: str, **kwargs: Any) -> Any:
            assert url == "https://www.googleapis.com/oauth2/v1/certs"
            return type(
                "CertificateResponse",
                (),
                {"status": 200, "data": json.dumps({"panel-test": public_key}).encode()},
            )()

    monkeypatch.setattr(oauth, "GoogleAuthRequest", LocalCertificates)

    def local_flow(
        self: GmailOAuthService,
        *,
        state: str | None = None,
        code_verifier: str | None = None,
        scopes: tuple[str, ...] | None = None,
    ) -> Flow:
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": "identity-route-client",
                    "client_secret": "identity-route-secret",
                    "auth_uri": f"{base_url}/local-provider/authorize",
                    "token_uri": f"{base_url}/local-provider/token",
                    "redirect_uris": [self.redirect_uri],
                }
            },
            scopes=list(scopes or ()),
            state=state,
            code_verifier=code_verifier,
            autogenerate_code_verifier=code_verifier is None,
        )
        flow.redirect_uri = self.redirect_uri
        return flow

    monkeypatch.setattr(GmailOAuthService, "_flow", local_flow)

    @context.app.get("/local-provider/authorize")
    async def provider_authorize(request: Request) -> HTMLResponse:
        query = dict(request.query_params)
        assert set(query["scope"].split()) == set(GOOGLE_ADMIN_SCOPES)
        assert query["prompt"] == "consent select_account"
        assert query["code_challenge_method"] == "S256"
        code = uuid4().hex
        grants[code] = query
        return HTMLResponse(
            '<h1>Локальный Gmail OAuth</h1><form method="post" action="/local-provider/grant">'
            f'<input type="hidden" name="code" value="{html.escape(code)}">'
            "<button>Разрешить Gmail</button></form>"
        )

    @context.app.post("/local-provider/grant")
    async def provider_grant(request: Request) -> RedirectResponse:
        form = await request.form()
        code = str(form["code"])
        grant = grants[code]
        return RedirectResponse(
            grant["redirect_uri"] + "?" + urlencode({"code": code, "state": grant["state"]}),
            status_code=303,
        )

    @context.app.post("/local-provider/token")
    async def provider_token(request: Request) -> dict[str, Any]:
        nonlocal token_exchanges
        form = await request.form()
        grant = grants.pop(str(form["code"]))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(str(form["code_verifier"]).encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert challenge == grant["code_challenge"]
        assert str(form["redirect_uri"]) == grant["redirect_uri"]
        token_exchanges += 1
        issued = int(time.time())
        token = jwt.encode(
            signer,
            {
                "iss": "https://accounts.google.com",
                "aud": "identity-route-client",
                "sub": "local-admin",
                "email": "admin@example.com",
                "email_verified": True,
                "iat": issued,
                "exp": issued + 3600,
                "nonce": grant["nonce"],
            },
            key_id="panel-test",
        ).decode()
        return {
            "access_token": "local-access-token",
            "refresh_token": "local-refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": grant["scope"],
            "id_token": token,
        }

    server = uvicorn.Server(
        uvicorn.Config(context.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    try:
        async with httpx.AsyncClient(base_url=base_url) as probe:
            for _ in range(100):
                try:
                    if (await probe.get("/login")).status_code == 200:
                        break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("local panel server failed to start")
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch()
            try:
                for attempt in range(3):
                    async with context.session_factory() as session:
                        preference = await session.scalar(
                            select(JobPreference).where(
                                JobPreference.profile_id == scenario.primary_id
                            )
                        )
                        assert preference is not None
                        preference.auto_send_enabled, preference.global_pause = False, False
                        user_preference = await session.scalar(
                            select(JobPreference).where(
                                JobPreference.profile_id == scenario.secondary_id
                            )
                        )
                        assert user_preference is not None
                        user_preference.auto_send_enabled, user_preference.global_pause = (
                            False,
                            False,
                        )
                        await GmailOAuthService(settings).disconnect(session)
                        await session.commit()
                    clean = await browser.new_context(viewport={"width": 1440, "height": 1000})
                    page = await clean.new_page()
                    await page.goto(f"{base_url}/login")
                    await page.locator("details summary").click()
                    await page.get_by_label("Пароль администратора").fill(PASSWORD)
                    await page.get_by_role("button", name="Войти", exact=True).click()
                    await expect(page).to_have_url(f"{base_url}/admin")
                    await page.get_by_label("Уведомления", exact=True).click()
                    await page.get_by_role("link", name="Автоотправка выключена").click()
                    await expect(page.locator("#auto-send")).to_be_in_viewport()
                    await page.get_by_role(
                        "button", name="Включить автоотправку", exact=True
                    ).click()
                    await (
                        page.get_by_role("dialog")
                        .get_by_role("button", name="Включить автоотправку", exact=True)
                        .click()
                    )
                    await expect(page.locator("#auto-send")).to_contain_text("Включена")
                    assert "view=settings" in page.url
                    await page.get_by_role("button", name="Поставить на паузу", exact=True).click()
                    await (
                        page.get_by_role("dialog")
                        .get_by_role("button", name="Поставить на паузу", exact=True)
                        .click()
                    )
                    await expect(page.locator("#auto-send")).to_contain_text("На паузе")
                    await page.get_by_role(
                        "button", name="Возобновить отправку", exact=True
                    ).click()
                    await (
                        page.get_by_role("dialog")
                        .get_by_role("button", name="Возобновить отправку", exact=True)
                        .click()
                    )
                    await expect(page.locator("#auto-send")).to_contain_text("Включена")
                    picker = page.locator("[data-profile-picker]")
                    await picker.locator("summary").click()
                    await expect(picker.locator("nav")).to_be_visible()
                    assert await picker.locator("nav").evaluate(
                        "el => { const r = el.getBoundingClientRect(); "
                        "return !!document.elementFromPoint(r.x+r.width/2, r.y+r.height/2)"
                        "?.closest('.profile-popover'); }"
                    )
                    assert await page.locator("select[name=profile_id]").count() == 0
                    assert (
                        await picker.locator("nav").evaluate(
                            "el => getComputedStyle(el).backgroundColor"
                        )
                        == "rgb(18, 25, 37)"
                    )
                    await page.screenshot(path=str(tmp_path / f"profile-dark-{attempt}.png"))
                    await page.keyboard.press("Escape")
                    await expect(picker.locator("nav")).not_to_be_visible()
                    await expect(picker.locator("summary")).to_be_focused()
                    await page.keyboard.press("Enter")
                    await picker.get_by_role("link", name="Второй профиль").click()
                    await expect(page.locator(".header-quota")).to_have_attribute(
                        "aria-label", "Отправлено сегодня 0 из 10"
                    )
                    sidebar = await page.locator(".sidebar-status").inner_text()
                    await page.get_by_role("link", name="Пользователи", exact=True).click()
                    assert f"profile_id={scenario.secondary_id}" in page.url
                    assert await page.locator(".sidebar-status").inner_text() == sidebar
                    await expect(
                        page.locator("main [aria-label='Управление доступом']")
                    ).to_be_visible()
                    await page.locator("[data-profile-picker] summary").click()
                    assert await page.locator(".profile-popover").evaluate(
                        "el => { const r = el.getBoundingClientRect(); "
                        "return !!document.elementFromPoint(r.x+r.width/2, r.y+r.height/2)"
                        "?.closest('.profile-popover'); }"
                    )
                    await (
                        page.locator(".profile-popover")
                        .get_by_role("link", name="Первый профиль")
                        .click()
                    )
                    assert "/admin/accounts?" in page.url
                    await expect(page.locator(".header-quota")).to_have_attribute(
                        "aria-label", "Отправлено сегодня 1 из 20"
                    )
                    await page.get_by_role("link", name="Настройки", exact=True).click()
                    await expect(page.locator(".header-quota")).to_have_attribute(
                        "aria-label", "Отправлено сегодня 1 из 20"
                    )
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await page.locator("[data-profile-picker] summary").click()
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= "
                        "document.documentElement.clientWidth"
                    )
                    await page.screenshot(path=str(tmp_path / f"profile-mobile-{attempt}.png"))
                    await page.keyboard.press("Escape")
                    await page.set_viewport_size({"width": 1440, "height": 1000})
                    if attempt == 1:
                        await page.goto(f"{base_url}/admin/auth/google?consent=1")
                    else:
                        await page.get_by_role("link", name="Подключить Gmail", exact=True).click()
                    await expect(
                        page.get_by_role("heading", name="Локальный Gmail OAuth")
                    ).to_be_visible()
                    await page.get_by_role("button", name="Разрешить Gmail").click()
                    await expect(page).to_have_url(
                        f"{base_url}/admin?view=overview&google=connected"
                    )
                    assert not any(
                        cookie["name"] == oauth.GMAIL_OAUTH_BINDING_COOKIE
                        for cookie in await clean.cookies()
                    )
                    async with context.session_factory() as session:
                        status = await GmailOAuthService(settings).get_status(session)
                        assert status["connected"] and status["delivery_ready"]
                        assert (
                            status["identity_verified"]
                            and status["identity_email"] == "admin@example.com"
                        )
                    await clean.close()
                    user_browser = await browser.new_context()
                    await user_browser.add_cookies(
                        [
                            {
                                "name": settings.user_session_cookie_name,
                                "value": AccountSessionSigner(
                                    settings.secret_key.get_secret_value()
                                ).issue(scenario.user_id, scenario.user_version),
                                "url": base_url,
                            }
                        ]
                    )
                    user_page = await user_browser.new_page()
                    await user_page.goto(
                        f"{base_url}/app?view=settings&profile_id={scenario.secondary_id}"
                    )
                    await user_page.get_by_role(
                        "button", name="Включить автоотправку", exact=True
                    ).click()
                    await (
                        user_page.get_by_role("dialog")
                        .get_by_role("button", name="Включить автоотправку", exact=True)
                        .click()
                    )
                    await expect(user_page.locator("#auto-send")).to_contain_text("Включена")
                    assert "view=settings" in user_page.url
                    await user_page.get_by_role(
                        "button", name="Поставить на паузу", exact=True
                    ).click()
                    await (
                        user_page.get_by_role("dialog")
                        .get_by_role("button", name="Поставить на паузу", exact=True)
                        .click()
                    )
                    await expect(user_page.locator("#auto-send")).to_contain_text("На паузе")
                    await user_page.get_by_role(
                        "button", name="Возобновить отправку", exact=True
                    ).click()
                    await (
                        user_page.get_by_role("dialog")
                        .get_by_role("button", name="Возобновить отправку", exact=True)
                        .click()
                    )
                    await expect(user_page.locator("#auto-send")).to_contain_text("Включена")
                    await user_browser.close()
                assert token_exchanges == 3
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await server_task
