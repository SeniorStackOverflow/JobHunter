from __future__ import annotations

import asyncio
import re
import socket
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import select

from app import cli
from app.crawlers.catalog import SourceDefinition
from app.crawlers.registry import build_default_registry
from app.models.entities import AuditEvent, JobSource
from app.models.enums import SourceHealth
from app.security.auth import SessionSigner, hash_password
from tests.integration.test_user_invite_auth import UserAuthContext
from tests.integration.test_user_invite_auth import user_auth_context as user_auth_context

pytestmark = pytest.mark.integration


async def _pending_source(context, monkeypatch, *, nested=False):
    monkeypatch.setattr(cli, "async_session_factory", context.session_factory)
    await cli.seed_defaults(include_fixture=False)
    async with context.session_factory() as session:
        source = await session.scalar(
            select(JobSource).where(JobSource.adapter_type == "delucru_md")
        )
        assert source is not None
        config = {
            "live_mode": True,
            "policy_review_acknowledged": False,
            "operator_setting": "keep",
        }
        source.configuration = {"source": config, "wrapper_setting": "keep"} if nested else config
        await session.commit()
        return source.id, dict(source.configuration)


def _admin(context):
    token = SessionSigner(context.settings.secret_key.get_secret_value()).issue(
        context.settings.admin_username
    )
    context.client.cookies.set(context.settings.session_cookie_name, token)
    from app.security.auth import CsrfProtector

    return CsrfProtector(context.settings.secret_key.get_secret_value()).issue(token)


@pytest.mark.parametrize("nested", [False, True])
async def test_policy_form_validates_preserves_settings_and_records_admin(
    user_auth_context, monkeypatch, nested
):
    context = user_auth_context
    source_id, original = await _pending_source(context, monkeypatch, nested=nested)
    csrf = _admin(context)
    blocked = await context.client.post(
        f"/admin/sources/{source_id}/toggle", data={"csrf_token": csrf}
    )
    assert (
        blocked.status_code == 303
        and "notice=source_policy_required" in blocked.headers["location"]
    )
    page = await context.client.get(blocked.headers["location"])
    assert page.status_code == 200 and "Сначала подтвердите проверку условий сайта" in page.text
    assert 'name="acknowledged"' in page.text
    for fields in (
        {},
        {"acknowledged": "true", "review_reference": "   "},
        {"acknowledged": "true", "review_reference": "x" * 501},
    ):
        invalid = await context.client.post(
            f"/admin/sources/{source_id}/policy-review", data={"csrf_token": csrf, **fields}
        )
        assert invalid.status_code == 303 and "source_policy_invalid" in invalid.headers["location"]
    async with context.session_factory() as session:
        source = await session.get(JobSource, source_id)
        assert source.configuration == original and source.enabled is False
    saved = await context.client.post(
        f"/admin/sources/{source_id}/policy-review",
        data={
            "csrf_token": csrf,
            "acknowledged": "true",
            "review_reference": "  operator-check-2026-10-03  ",
        },
    )
    assert saved.status_code == 303 and "source_policy_saved" in saved.headers["location"]
    async with context.session_factory() as session:
        source = await session.get(JobSource, source_id)
        config = source.configuration["source"] if nested else source.configuration
        assert config["policy_review_acknowledged"] is True
        assert config["policy_review_reference"] == "operator-check-2026-10-03"
        assert config["operator_setting"] == "keep"
        if nested:
            assert source.configuration["wrapper_setting"] == "keep"
        assert source.enabled is False and source.automatic_actions_paused is True
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "source.policy_reviewed")
        )
        assert audit.actor == "admin"
        assert audit.sanitized_details["policy_review_reference"] == "operator-check-2026-10-03"
    enabled = await context.client.post(
        f"/admin/sources/{source_id}/toggle", data={"csrf_token": csrf}
    )
    assert enabled.status_code == 303 and "source_enabled" in enabled.headers["location"]
    async with context.session_factory() as session:
        source = await session.get(JobSource, source_id)
        assert source.enabled is True and source.automatic_actions_paused is True
        assert source.health_status == SourceHealth.UNKNOWN


async def test_policy_form_requires_admin_and_csrf(user_auth_context, monkeypatch):
    context = user_auth_context
    source_id, original = await _pending_source(context, monkeypatch)
    fields = {
        "csrf_token": "wrong",
        "acknowledged": "true",
        "review_reference": "check",
        "enable": "true",
    }
    assert (
        await context.client.post(f"/admin/sources/{source_id}/policy-review", data=fields)
    ).status_code == 401
    _admin(context)
    assert (
        await context.client.post(f"/admin/sources/{source_id}/policy-review", data=fields)
    ).status_code == 403
    async with context.session_factory() as session:
        source = await session.get(JobSource, source_id)
        assert source.configuration == original and source.enabled is False


@pytest.mark.e2e
async def test_startup_catalog_and_source_confirmation_three_clean_browsers(
    user_auth_context: UserAuthContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from mcp.server.fastmcp import FastMCP
    from playwright.async_api import async_playwright, expect

    from app import main as main_module
    from app.crawlers import registry as registry_module
    from app.crawlers.adapters.delucru_md import DelucruMdAdapter
    from app.crawlers.adapters.fixture_source import FixtureSourceAdapter
    from app.database import session as database_session

    context = user_auth_context
    monkeypatch.setattr(cli, "async_session_factory", context.session_factory)
    await cli.seed_defaults(include_fixture=False)
    async with context.session_factory() as session:
        missing = await session.scalar(
            select(JobSource).where(JobSource.adapter_type == "delucru_md")
        )
        await session.delete(missing)
        await session.commit()
    registry = build_default_registry()
    original = registry.source_definitions()["delucru_md"]
    registry.register(
        "delucru_md",
        DelucruMdAdapter,
        source=replace(
            original,
            configuration={
                **original.configuration,
                "policy_review_acknowledged": False,
                "policy_review_reference": None,
            },
        ),
    )
    registry.register(
        "future_board",
        FixtureSourceAdapter,
        source=SourceDefinition(
            key="future-site", name="Future board", base_url="https://future.example.test"
        ),
    )
    monkeypatch.setattr(registry_module, "build_default_registry", lambda: registry)
    monkeypatch.setattr(database_session, "async_session_factory", context.session_factory)
    monkeypatch.setattr(main_module, "settings", context.settings)
    # A real, fresh MCP transport per local application lifecycle.
    monkeypatch.setattr(main_module, "mcp_asgi", FastMCP("source-panel-test").streamable_http_app())
    monkeypatch.setattr(context.app.router, "lifespan_context", main_module.lifespan)
    password = "source-panel-local-test"
    context.settings.admin_password_hash = SecretStr(hash_password(password))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://localhost:{port}"
    context.settings.public_base_url = origin
    server = uvicorn.Server(
        uvicorn.Config(context.app, host="127.0.0.1", port=port, log_level="error")
    )
    task = asyncio.create_task(server.serve())
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    if (await client.get(origin + "/login")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("source panel server did not start")
        async with context.session_factory() as session:
            sources = list((await session.scalars(select(JobSource))).all())
            assert {source.catalog_key for source in sources} == {
                definition.key for definition in registry.source_definitions().values()
            }
            delucru = next(source for source in sources if source.catalog_key == "delucru_md")
            source_id = delucru.id
            future = next(source for source in sources if source.catalog_key == "future-site")
            assert future.enabled is False and future.automatic_actions_paused is True
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(headless=True)
            try:
                for index in range(3):
                    async with context.session_factory() as session:
                        source = await session.get(JobSource, source_id)
                        source.enabled = False
                        source.configuration = {
                            "live_mode": True,
                            "policy_review_acknowledged": False,
                            "operator_setting": "keep",
                        }
                        await session.commit()
                    browser_context = await browser.new_context(
                        viewport={"width": 390 if index == 1 else 1440, "height": 900}
                    )
                    try:
                        page = await browser_context.new_page()
                        errors = []
                        page.on(
                            "pageerror",
                            lambda error, errors=errors: errors.append(type(error).__name__),
                        )
                        await page.goto(origin + "/login")
                        await page.locator(".login-admin-fallback summary").click()
                        await page.locator('input[name="password"]').fill(password)
                        await page.locator('.login-password-form button[type="submit"]').click()
                        await expect(page).to_have_url(origin + "/admin")
                        await page.goto(origin + "/admin?view=settings")
                        await expect(page.locator("#sources .compact-row")).to_have_count(
                            len(registry.source_definitions())
                        )
                        await expect(page.locator("#sources")).to_contain_text("Future board")
                        controls = page.locator(f"#source-controls-{source_id}")
                        await controls.get_by_role("button", name="Запустить", exact=True).click()
                        await expect(page.locator("[data-confirm-dialog]")).to_be_visible()
                        await page.locator("[data-confirm-accept]").click()
                        await expect(page).to_have_url(re.compile("notice=source_policy_required"))
                        await expect(page.locator("[data-action-notice]")).to_contain_text(
                            "Сначала подтвердите проверку условий сайта"
                        )
                        policy = page.locator(f"#source-policy-{source_id}")
                        await expect(policy).to_be_visible()
                        dimensions = await page.evaluate(
                            "({width: innerWidth, scroll: document.documentElement.scrollWidth})"
                        )
                        assert dimensions["scroll"] <= dimensions["width"] + 2
                        await policy.screenshot(
                            path=str(tmp_path / f"source-confirmation-{index}.png")
                        )
                        await policy.locator('input[name="acknowledged"]').check()
                        await policy.locator('input[name="review_reference"]').fill(
                            f"local-operator-check-{index}"
                        )
                        await policy.get_by_role(
                            "button", name="Сохранить и запустить обход"
                        ).click()
                        await expect(page).to_have_url(re.compile("notice=source_enabled"))
                        await expect(page.locator("[data-action-notice]")).to_contain_text(
                            "Источник включён"
                        )
                        await expect(page.locator(f"#source-controls-{source_id}")).to_contain_text(
                            "Обход включён"
                        )
                        assert not errors
                        dimensions = await page.evaluate(
                            "({width: innerWidth, scroll: document.documentElement.scrollWidth})"
                        )
                        assert dimensions["scroll"] <= dimensions["width"] + 2
                        async with context.session_factory() as session:
                            source = await session.get(JobSource, source_id)
                            assert (
                                source.enabled is True and source.automatic_actions_paused is True
                            )
                            assert source.configuration["policy_review_acknowledged"] is True
                            assert (
                                source.configuration["policy_review_reference"]
                                == f"local-operator-check-{index}"
                            )
                            assert source.configuration["operator_setting"] == "keep"
                    finally:
                        await browser_context.close()
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
