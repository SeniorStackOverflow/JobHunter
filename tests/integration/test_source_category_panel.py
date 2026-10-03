"""The settings page lets the owner pick categories per source from the
categories that source publishes, instead of typing slugs into text fields."""

from __future__ import annotations

# Russian labels are assertions against the rendered interface.
import asyncio
import re
import socket
from uuid import UUID

import httpx
import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import select

from app.models.entities import (
    Account,
    JobPreference,
    JobSource,
    ProfileSourcePreference,
    SourceCategory,
    UserProfile,
)
from app.models.enums import AccountRole, AccountStatus
from app.profiles.service import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.security.auth import AccountSessionSigner, SessionSigner
from tests.integration.test_user_invite_auth import UserAuthContext
from tests.integration.test_user_invite_auth import user_auth_context as user_auth_context

pytestmark = pytest.mark.integration

_CATALOG = (
    ("others", "Работа без опыта"),
    ("warehouses", "Складское хозяйство"),
    ("workers", "Разнорабочие, грузчики"),
    ("calls", "Работа на телефоне, колл-центры"),
)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_registered_default_sources_are_visible_in_both_panels_three_clean_contexts(
    user_auth_context: UserAuthContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from playwright.async_api import async_playwright, expect

    from app import cli
    from app.admin import routes as admin_routes
    from app.api import routes as api_routes
    from app.auth import access as auth_access
    from app.security.auth import hash_password

    monkeypatch.setattr(cli, "async_session_factory", user_auth_context.session_factory)
    await cli.seed_defaults(include_fixture=False)
    async with user_auth_context.session_factory() as session:
        # Reproduce the deployed database: Rabota exists, Delucru is missing.
        delucru = await session.scalar(
            select(JobSource).where(JobSource.adapter_type == "delucru_md")
        )
        assert delucru is not None
        await session.delete(delucru)
        owner = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
        session.add(owner)
        await session.flush()
        profile = UserProfile(name="Source registration user", owner_account_id=owner.id)
        session.add(profile)
        await session.flush()
        session.add(JobPreference(profile_id=profile.id))
        owner_id, profile_id = owner.id, profile.id
        await session.commit()
    await cli.seed_defaults(include_fixture=False)
    await cli.seed_defaults(include_fixture=False)
    async with user_auth_context.session_factory() as session:
        delucru = await session.scalar(
            select(JobSource).where(JobSource.adapter_type == "delucru_md")
        )
        assert delucru is not None and delucru.enabled is False
        assert delucru.automatic_actions_paused is True
        delucru_id = delucru.id

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://localhost:{port}"
    password = "local-source-registration-test"
    settings = user_auth_context.settings.model_copy(
        update={
            "public_base_url": base_url,
            "admin_password_hash": SecretStr(hash_password(password)),
        }
    )
    for module in (admin_routes, api_routes, auth_access):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    server = uvicorn.Server(
        uvicorn.Config(user_auth_context.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    if (await client.get(base_url + "/login")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("source registration browser server did not start")
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(headless=True)
            try:
                for _ in range(3):
                    for role in ("admin", "user"):
                        context = await browser.new_context()
                        try:
                            page = await context.new_page()
                            if role == "admin":
                                await page.goto(base_url + "/login")
                                await page.locator(".login-admin-fallback summary").click()
                                await page.locator('input[name="password"]').fill(password)
                                await page.locator(
                                    '.login-password-form button[type="submit"]'
                                ).click()
                                await expect(page).to_have_url(base_url + "/admin")
                                route = "/admin?view=settings"
                            else:
                                token = AccountSessionSigner(
                                    settings.secret_key.get_secret_value()
                                ).issue(owner_id, 0)
                                await context.add_cookies(
                                    [
                                        {
                                            "name": settings.user_session_cookie_name,
                                            "value": token,
                                            "url": base_url,
                                        }
                                    ]
                                )
                                route = f"/app?view=settings&profile_id={profile_id}"
                            response = await page.goto(base_url + route)
                            assert response is not None and response.status == 200
                            sources = page.locator("#sources")
                            await expect(sources.locator(".compact-row")).to_have_count(2)
                            await expect(sources).to_contain_text("Rabota.md")
                            row = sources.locator(f"#source-{delucru_id}")
                            await expect(row).to_contain_text("Delucru.md")
                            await expect(row).to_contain_text(
                                "Администратор временно остановил обход"
                            )
                        finally:
                            await context.close()
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)


async def _seed(context: UserAuthContext, *, owner: Account | None = None) -> tuple[UUID, UUID]:
    async with context.session_factory() as session:
        if owner is not None:
            session.add(owner)
            await session.flush()
        profile = UserProfile(
            name="Candidate",
            is_default=owner is None,
            owner_account_id=owner.id if owner is not None else BOOTSTRAP_ADMIN_ACCOUNT_ID,
        )
        source = JobSource(
            name="Rabota.md", base_url="https://www.rabota.md", adapter_type="rabota_md"
        )
        session.add_all([profile, source])
        await session.flush()
        session.add(
            JobPreference(
                profile_id=profile.id,
                allowed_categories=["others", "warehouses"],
                auto_send_categories=["warehouses"],
                allowed_cities=["Chisinau"],
            )
        )
        for external_id, name in _CATALOG:
            session.add(
                SourceCategory(
                    source_id=source.id,
                    external_id=external_id,
                    name=name,
                    url=f"https://www.rabota.md/ru/vacancies/category/{external_id}",
                    locale="ru",
                )
            )
        await session.commit()
        return profile.id, source.id


def _as_admin(context: UserAuthContext) -> None:
    context.client.cookies.set(
        context.settings.session_cookie_name,
        SessionSigner(context.settings.secret_key.get_secret_value()).issue(
            context.settings.admin_username
        ),
    )


def _csrf(page_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page_text)
    assert match is not None
    return match.group(1)


def _checked_state(page_text: str, external_id: str) -> str:
    match = re.search(rf'name="category:{re.escape(external_id)}" value="(\w+)" checked', page_text)
    assert match is not None, external_id
    return match.group(1)


@pytest.mark.asyncio
async def test_settings_offer_the_sources_categories_instead_of_text_fields(
    user_auth_context: UserAuthContext,
) -> None:
    profile_id, source_id = await _seed(user_auth_context)
    _as_admin(user_auth_context)

    page = await user_auth_context.client.get(
        "/admin", params={"view": "settings", "profile_id": str(profile_id)}
    )

    assert page.status_code == 200
    for _external_id, name in _CATALOG:
        assert name in page.text
    assert f'action="/admin/profile-sources/{source_id}/categories"' in page.text
    # The profile-wide lists are shown as the starting choice.
    assert _checked_state(page.text, "warehouses") == "auto"
    assert _checked_state(page.text, "others") == "search"
    assert _checked_state(page.text, "workers") == "off"
    for legacy in ("allowed_categories", "auto_send_categories", "forbidden_categories"):
        assert f'name="{legacy}"' not in page.text


@pytest.mark.asyncio
async def test_admin_saves_a_category_choice_for_one_source(
    user_auth_context: UserAuthContext,
) -> None:
    profile_id, source_id = await _seed(user_auth_context)
    _as_admin(user_auth_context)
    page = await user_auth_context.client.get(
        "/admin", params={"view": "settings", "profile_id": str(profile_id)}
    )

    response = await user_auth_context.client.post(
        f"/admin/profile-sources/{source_id}/categories",
        data={
            "profile_id": str(profile_id),
            "csrf_token": _csrf(page.text),
            "category:others": "search",
            "category:warehouses": "auto",
            "category:workers": "auto",
            "category:calls": "excluded",
        },
    )

    assert response.status_code == 303
    assert "notice=source_categories_saved" in response.headers["location"]
    async with user_auth_context.session_factory() as session:
        row = await session.get(ProfileSourcePreference, (profile_id, source_id))
        assert row is not None and row.categories_configured is True
        assert row.search_categories == ["others", "warehouses", "workers"]
        assert row.auto_send_categories == ["warehouses", "workers"]
        assert row.excluded_categories == ["calls"]
    saved = await user_auth_context.client.get(response.headers["location"])
    assert saved.status_code == 200
    assert _checked_state(saved.text, "workers") == "auto"
    assert _checked_state(saved.text, "calls") == "excluded"
    assert "Категории источника сохранены" in saved.text


@pytest.mark.asyncio
async def test_a_category_the_source_does_not_publish_is_refused(
    user_auth_context: UserAuthContext,
) -> None:
    profile_id, source_id = await _seed(user_auth_context)
    _as_admin(user_auth_context)
    page = await user_auth_context.client.get(
        "/admin", params={"view": "settings", "profile_id": str(profile_id)}
    )

    response = await user_auth_context.client.post(
        f"/admin/profile-sources/{source_id}/categories",
        data={
            "profile_id": str(profile_id),
            "csrf_token": _csrf(page.text),
            "category:sklad": "search",
        },
    )

    assert response.status_code == 422
    async with user_auth_context.session_factory() as session:
        assert await session.get(ProfileSourcePreference, (profile_id, source_id)) is None


@pytest.mark.asyncio
async def test_saving_the_criteria_form_keeps_the_category_choice(
    user_auth_context: UserAuthContext,
) -> None:
    profile_id, _source_id = await _seed(user_auth_context)
    _as_admin(user_auth_context)
    page = await user_auth_context.client.get(
        "/admin", params={"view": "settings", "profile_id": str(profile_id)}
    )

    response = await user_auth_context.client.post(
        "/admin/preferences",
        data={
            "profile_id": str(profile_id),
            "csrf_token": _csrf(page.text),
            "allowed_cities": "Chisinau, Balti",
            "maximum_daily_applications": "20",
            "minimum_auto_send_score": "70",
        },
    )

    assert response.status_code == 303
    async with user_auth_context.session_factory() as session:
        preference = await session.scalar(
            select(JobPreference).where(JobPreference.profile_id == profile_id)
        )
        assert preference is not None
        assert preference.allowed_cities == ["Chisinau", "Balti"]
        assert preference.allowed_categories == ["others", "warehouses"]
        assert preference.auto_send_categories == ["warehouses"]


@pytest.mark.asyncio
async def test_user_saves_categories_only_for_an_own_profile(
    user_auth_context: UserAuthContext,
) -> None:
    owner = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
    profile_id, source_id = await _seed(user_auth_context, owner=owner)
    stranger = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
    async with user_auth_context.session_factory() as session:
        session.add(stranger)
        await session.flush()
        stranger_profile = UserProfile(name="Stranger profile", owner_account_id=stranger.id)
        session.add(stranger_profile)
        await session.commit()
        stranger_profile_id = stranger_profile.id
    signer = AccountSessionSigner(user_auth_context.settings.secret_key.get_secret_value())
    user_auth_context.client.cookies.set(
        user_auth_context.settings.user_session_cookie_name,
        signer.issue(owner.id, owner.session_version),
    )
    page = await user_auth_context.client.get(
        "/app", params={"view": "settings", "profile_id": str(profile_id)}
    )
    assert page.status_code == 200
    assert f'action="/app/profiles/{profile_id}/sources/{source_id}/categories"' in page.text
    token = _csrf(page.text)

    own = await user_auth_context.client.post(
        f"/app/profiles/{profile_id}/sources/{source_id}/categories",
        data={"csrf_token": token, "category:workers": "search"},
    )
    foreign = await user_auth_context.client.post(
        f"/app/profiles/{stranger_profile_id}/sources/{source_id}/categories",
        data={"csrf_token": token, "category:workers": "search"},
    )

    assert own.status_code == 303
    assert foreign.status_code == 404
    async with user_auth_context.session_factory() as session:
        row = await session.get(ProfileSourcePreference, (profile_id, source_id))
        assert row is not None and row.search_categories == ["workers"]
        assert await session.get(ProfileSourcePreference, (stranger_profile_id, source_id)) is None


@pytest.mark.asyncio
async def test_a_source_without_published_categories_says_so(
    user_auth_context: UserAuthContext,
) -> None:
    profile_id, _source_id = await _seed(user_auth_context)
    async with user_auth_context.session_factory() as session:
        session.add(JobSource(name="Feed", base_url="https://feed.example", adapter_type="rss"))
        await session.commit()
    _as_admin(user_auth_context)

    page = await user_auth_context.client.get(
        "/admin", params={"view": "settings", "profile_id": str(profile_id)}
    )

    assert "Источник не публикует категории" in page.text
