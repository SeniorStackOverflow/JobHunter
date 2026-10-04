from __future__ import annotations

# Russian UI copy is intentional.
# ruff: noqa: RUF001
import asyncio
import re
import socket
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import delete, select

from app import cli
from app.crawlers.catalog import SourceDefinition
from app.crawlers.pipeline import ScanService
from app.crawlers.registry import build_default_registry
from app.models.entities import Account, AuditEvent, JobSource, ScanRun, SourceJob, UserProfile
from app.models.enums import AccountRole, AccountStatus, RunStatus, ScanType, SourceHealth
from app.security.auth import AccountSessionSigner, SessionSigner, hash_password
from tests.integration.test_user_invite_auth import UserAuthContext
from tests.integration.test_user_invite_auth import user_auth_context as user_auth_context
from tests.unit.test_delucru_adapter import FixtureFetcher, finite_category_routes, fixture

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def offline_scan_queue(user_auth_context, monkeypatch):
    from app.database import session as database_session
    from app.scheduler.tasks import run_scan_task

    queued = []
    monkeypatch.setattr(
        database_session, "async_session_factory", user_auth_context.session_factory
    )
    monkeypatch.setattr(run_scan_task, "delay", queued.append)
    return queued


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
        scan = await session.scalar(select(ScanRun).where(ScanRun.source_id == source_id))
        assert scan.scan_type == ScanType.FULL and scan.status == RunStatus.QUEUED


@pytest.mark.parametrize("entry", ["toggle", "policy-review"])
async def test_enable_queues_initial_full_and_reenable_uses_incremental(
    user_auth_context, monkeypatch, offline_scan_queue, entry
):
    context = user_auth_context
    source_id, _ = await _pending_source(context, monkeypatch)
    csrf = _admin(context)
    async with context.session_factory() as session:
        source = await session.get(JobSource, source_id)
        source.configuration = {
            **source.configuration,
            "policy_review_acknowledged": True,
            "policy_review_reference": "local-review",
        }
        await session.commit()
    fields = {
        "csrf_token": csrf,
        "acknowledged": "true",
        "review_reference": "local-review",
        "enable": "true",
    }
    response = await context.client.post(f"/admin/sources/{source_id}/{entry}", data=fields)
    assert response.status_code == 303 and "source_enabled" in response.headers["location"]
    async with context.session_factory() as session:
        run = await session.scalar(select(ScanRun).where(ScanRun.source_id == source_id))
        assert run.scan_type == ScanType.FULL and run.status == RunStatus.QUEUED
        assert offline_scan_queue == [str(run.id)]
        run.status = RunStatus.RUNNING
        await session.commit()
    # Clicking a manual retry while the first scan is running must reuse it.
    retry = await context.client.post(
        f"/admin/sources/{source_id}/scan/full", data={"csrf_token": csrf}
    )
    assert retry.status_code == 303 and "source_scan_active" in retry.headers["location"]
    assert len(offline_scan_queue) == 1
    async with context.session_factory() as session:
        run = await session.get(ScanRun, run.id)
        run.status = RunStatus.SUCCEEDED
        await session.commit()
    await context.client.post(f"/admin/sources/{source_id}/toggle", data={"csrf_token": csrf})
    response = await context.client.post(
        f"/admin/sources/{source_id}/toggle", data={"csrf_token": csrf}
    )
    assert "source_enabled" in response.headers["location"]
    async with context.session_factory() as session:
        runs = list(
            (await session.scalars(select(ScanRun).where(ScanRun.source_id == source_id))).all()
        )
        assert len(runs) == 2
        incremental = next(run for run in runs if run.scan_type == ScanType.INCREMENTAL)
        assert incremental.status == RunStatus.QUEUED
        assert offline_scan_queue[-1] == str(incremental.id)
        source = await session.get(JobSource, source_id)
        assert source.enabled and source.automatic_actions_paused


@pytest.mark.parametrize("entry", ["toggle", "policy-review"])
async def test_enable_queue_failure_is_visible_and_initial_full_can_be_retried(
    user_auth_context, monkeypatch, offline_scan_queue, entry
):
    from app.scheduler.tasks import run_scan_task

    context = user_auth_context
    source_id, _ = await _pending_source(context, monkeypatch)
    csrf = _admin(context)
    async with context.session_factory() as session:
        source = await session.get(JobSource, source_id)
        source.configuration = {
            **source.configuration,
            "policy_review_acknowledged": True,
            "policy_review_reference": "local-review",
        }
        await session.commit()

    def unavailable(_):
        raise RuntimeError("offline broker failure")

    monkeypatch.setattr(run_scan_task, "delay", unavailable)
    response = await context.client.post(
        f"/admin/sources/{source_id}/{entry}",
        data={
            "csrf_token": csrf,
            "acknowledged": "true",
            "review_reference": "local-review",
            "enable": "true",
        },
    )
    assert (
        response.status_code == 303 and "source_queue_unavailable" in response.headers["location"]
    )
    page = await context.client.get(response.headers["location"])
    assert "Проверка не удалась" in page.text
    assert f"/admin/sources/{source_id}/scan/full" in page.text
    async with context.session_factory() as session:
        run = await session.scalar(select(ScanRun).where(ScanRun.source_id == source_id))
        assert run.status == RunStatus.FAILED and run.finished_at is not None
        source = await session.get(JobSource, source_id)
        assert source.enabled and source.automatic_actions_paused
    monkeypatch.setattr(run_scan_task, "delay", offline_scan_queue.append)
    response = await context.client.post(
        f"/admin/sources/{source_id}/scan/full", data={"csrf_token": csrf}
    )
    assert "scan_started" in response.headers["location"]
    assert len(offline_scan_queue) == 1


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


async def test_scan_status_requires_admin_and_handles_missing_source(
    user_auth_context, monkeypatch
):
    from uuid import uuid4

    context = user_auth_context
    source_id, _ = await _pending_source(context, monkeypatch)
    url = f"/admin/sources/{source_id}/scan-status"
    assert (await context.client.get(url)).status_code == 401
    _admin(context)
    state = await context.client.get(url)
    assert state.status_code == 200
    assert state.json()["active"] is False
    assert state.json()["enabled"] is False
    assert (await context.client.get(f"/admin/sources/{uuid4()}/scan-status")).status_code == 404


@pytest.mark.e2e
async def test_startup_catalog_and_source_confirmation_three_clean_browsers(
    user_auth_context: UserAuthContext,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    offline_scan_queue,
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
    fetchers = []
    detail_layout = {"current": False}

    def local_fetcher(_):
        routes = finite_category_routes()
        detail = fixture("job_current_detail_ro.html")
        if not detail_layout["current"]:
            detail = detail.replace('class="employer-details-page"', 'class="old-layout"')
        else:
            detail = detail.replace(
                '<div id="job-description">',
                '<div class="col">Salariu: 999999 USD</div><div id="job-description">',
            )
        routes["https://www.delucru.md/job/junior-data-scientist-88409"] = detail
        fetcher = FixtureFetcher(routes)
        fetchers.append(fetcher)
        return fetcher

    registry = build_default_registry(client_factory=local_fetcher)
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
            account = Account(role=AccountRole.USER, status=AccountStatus.ACTIVE)
            session.add(account)
            await session.flush()
            session.add(UserProfile(name="Source panel user", owner_account_id=account.id))
            await session.commit()
            user_token = AccountSessionSigner(context.settings.secret_key.get_secret_value()).issue(
                account.id, account.session_version
            )
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(headless=True)

            async def check_user_panel(label, width):
                user_browser = await browser.new_context(viewport={"width": width, "height": 900})
                try:
                    await user_browser.add_cookies(
                        [
                            {
                                "name": context.settings.user_session_cookie_name,
                                "value": user_token,
                                "url": origin,
                            }
                        ]
                    )
                    user_page = await user_browser.new_page()
                    response = await user_page.goto(origin + "/app?view=settings")
                    assert response.status == 200
                    await expect(user_page.locator(f"#source-{source_id}")).to_contain_text(label)
                    await expect(user_page.locator("#source-management")).to_have_count(0)
                    assert await user_page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 2"
                    )
                finally:
                    await user_browser.close()

            try:
                for index in range(3):
                    detail_layout["current"] = False
                    async with context.session_factory() as session:
                        await session.execute(delete(ScanRun).where(ScanRun.source_id == source_id))
                        source = await session.get(JobSource, source_id)
                        source.enabled = False
                        source.health_status = SourceHealth.PAUSED
                        source.automatic_actions_paused = True
                        source.last_scan_status = None
                        source.configuration = {
                            "live_mode": True,
                            "locale_priority": ["ro"],
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
                        await expect(page.locator(f"#source-{source_id}")).to_contain_text(
                            "Обход: В очереди"
                        )
                        await check_user_panel("Обход: В очереди", 390 if index == 1 else 1440)
                        async with context.session_factory() as session:
                            queued = await session.scalar(
                                select(ScanRun).where(
                                    ScanRun.source_id == source_id,
                                    ScanRun.status == RunStatus.QUEUED,
                                )
                            )
                            assert queued is not None
                            assert queued.scan_type == ScanType.FULL
                            assert offline_scan_queue[-1] == str(queued.id)
                        scan_buttons = page.locator(f'[data-source-scan="{source_id}"] button')
                        await expect(scan_buttons).to_have_count(2)
                        for button in await scan_buttons.all():
                            await expect(button).to_be_disabled()
                            await expect(button).to_contain_text("Полный обход: В очереди")
                        # A second tab/stale form cannot publish the queued scan again,
                        # including a request for a different scan type.
                        published = len(offline_scan_queue)
                        for scan_type in ("full", "incremental"):
                            csrf = await page.locator(
                                'input[name="csrf_token"]'
                            ).first.input_value()
                            retry = await browser_context.request.post(
                                origin + f"/admin/sources/{source_id}/scan/{scan_type}",
                                form={"csrf_token": csrf},
                                max_redirects=0,
                            )
                            assert "source_scan_active" in retry.headers["location"]
                        assert len(offline_scan_queue) == published
                        async with context.session_factory() as session:
                            active = await session.get(ScanRun, queued.id)
                            active.status = RunStatus.RUNNING
                            await session.commit()
                        for button in await scan_buttons.all():
                            await expect(button).to_contain_text(
                                "Полный обход: Выполняется", timeout=12000
                            )
                        # Return it to the queue for the real offline pipeline to claim.
                        async with context.session_factory() as session:
                            active = await session.get(ScanRun, queued.id)
                            active.status = RunStatus.QUEUED
                            await session.commit()
                        # Run the actual adapter/pipeline entirely against saved local HTML.
                        fetchers.clear()
                        completed = await ScanService(context.session_factory, registry).run_scan(
                            queued.id
                        )
                        assert completed.status == RunStatus.SUCCEEDED
                        assert completed.found_jobs > 0
                        assert not any(
                            url.endswith("acquisitions?page=3")
                            for fetcher in fetchers
                            for url in fetcher.requested
                        )
                        # Polling unlocks both controls and updates health without reloading.
                        for button in await scan_buttons.all():
                            await expect(button).to_be_enabled(timeout=12000)
                        await expect(page.locator(f"#source-{source_id}")).to_contain_text(
                            "Работает"
                        )
                        await check_user_panel("Работает", 390 if index == 1 else 1440)
                        await expect(
                            page.locator(f"#source-{source_id} [action$='/scan/incremental']")
                        ).to_have_count(1)
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
                            assert source.health_status == SourceHealth.HEALTHY
                        # Reproduce the cursor saved by the previous adapter after it
                        # fabricated page 3, then continue through the actual UI route.
                        async with context.session_factory() as session:
                            saved = await session.get(ScanRun, completed.id)
                            old_job = await session.scalar(
                                select(SourceJob).where(
                                    SourceJob.source_id == source_id,
                                    SourceJob.external_job_id == "88409",
                                )
                            )
                            assert old_job.salary_text is None and old_job.no_experience is None
                            old_job.raw_metadata = {
                                key: value
                                for key, value in old_job.raw_metadata.items()
                                if key != "detail_normalization_version"
                            }
                            saved.status = RunStatus.PARTIAL
                            checkpoint = dict(saved.checkpoint)
                            checkpoint.update(
                                page_url="https://www.delucru.md/jobs/acquisitions?page=3",
                                entrypoint_index=0,
                                completed_entrypoints=[],
                            )
                            adapter_state = dict(checkpoint.get("adapter_state", {}))
                            adapter_state["scan_entrypoints"] = [
                                {
                                    "url": "https://www.delucru.md/jobs/acquisitions",
                                    "category": "acquisitions",
                                    "region": None,
                                },
                                {
                                    "url": "https://www.delucru.md/jobs",
                                    "category": None,
                                    "region": None,
                                },
                            ]
                            checkpoint["adapter_state"] = adapter_state
                            saved.checkpoint = checkpoint
                            source = await session.get(JobSource, source_id)
                            source.health_status = SourceHealth.DEGRADED
                            source.last_scan_status = RunStatus.PARTIAL
                            source.automatic_actions_paused = True
                            await session.commit()
                        detail_layout["current"] = True
                        await page.reload()
                        await page.locator(
                            f"#source-controls-{source_id} [data-source-scan] button"
                        ).click()
                        await expect(page).to_have_url(re.compile("notice=scan_started"))
                        async with context.session_factory() as session:
                            continued = await session.scalar(
                                select(ScanRun).where(
                                    ScanRun.source_id == source_id,
                                    ScanRun.status == RunStatus.QUEUED,
                                )
                            )
                            assert continued is not None
                            assert continued.diagnostics["resume_parent_scan_id"] == str(
                                completed.id
                            )
                            assert continued.checkpoint["page_url"].endswith("acquisitions?page=3")
                        recovered = await ScanService(context.session_factory, registry).run_scan(
                            continued.id
                        )
                        assert recovered.status == RunStatus.SUCCEEDED
                        assert recovered.new_jobs == 0 and recovered.found_jobs == 1
                        assert recovered.updated_jobs == 1
                        async with context.session_factory() as session:
                            restored = await session.scalar(
                                select(SourceJob).where(
                                    SourceJob.source_id == source_id,
                                    SourceJob.external_job_id == "88409",
                                )
                            )
                            assert str(restored.salary_min) == "12000.00"
                            assert restored.no_experience is True
                            assert restored.location == "Chișinău"
                            assert restored.employment_type == "full-time"
                            assert restored.raw_metadata["detail_normalization_version"] == 1
                        for button in await page.locator(
                            f'[data-source-scan="{source_id}"] button'
                        ).all():
                            await expect(button).to_be_enabled(timeout=12000)
                        await expect(page.locator(f"#source-{source_id}")).to_contain_text(
                            "Работает"
                        )
                        await check_user_panel("Работает", 390 if index == 1 else 1440)
                    finally:
                        await browser_context.close()
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
