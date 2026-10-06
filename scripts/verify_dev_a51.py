"""Acceptance against the actual A51 deployment; no live providers are used."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from dev_a51 import STATE, WEB_PORT, compose, connect  # noqa: E402

BASE = f"http://127.0.0.1:{WEB_PORT}"

# These accounts exist only in the isolated DEV database. Signing a test session
# exercises real ownership/session checks without contacting Google's live OAuth.
PREPARE_ACCOUNTS = """
import asyncio, json
from uuid import UUID
from app.database import async_session_factory
from app.models.entities import Account, JobSource
from app.models.enums import AccountRole
from app.profiles import ProfileService
from app.profiles.schemas import UserProfileInput
from app.security.auth import AccountSessionSigner
from app.settings import get_settings
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from app.crawlers.registry import build_default_registry
async def main():
    settings = get_settings()
    assert settings.environment == "development"
    assert settings.email_provider == "fake"
    assert settings.llm_provider == "mock"
    assert not settings.real_email_delivery_enabled
    assert not settings.phone_agent_enabled
    assert not settings.telegram_enabled
    async with async_session_factory() as session:
        versions = await session.execute(text("SELECT version_num FROM alembic_version"))
        actual_heads = set(versions.scalars())
        expected_heads = set(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
        assert actual_heads == expected_heads, "DEV database migrations are incomplete"
        actual_keys = set((await session.scalars(select(JobSource.catalog_key))).all())
        definitions = build_default_registry().source_definitions()
        expected_keys = {item.key for item in definitions.values()}
        assert expected_keys <= actual_keys, "Built-in sources are absent from the DEV database"
        accounts = []
        for n in (1, 2):
            account_id = UUID(f"a51de000-0000-4000-8000-{n:012d}")
            account = await session.get(Account, account_id)
            if account is None:
                account = Account(id=account_id, role=AccountRole.USER)
                session.add(account)
                await session.flush()
            profile = await ProfileService().get_profile(session, owner_account_id=account_id)
            if profile is None:
                profile = await ProfileService().create_profile(session,
                    UserProfileInput(name=f"A51 DEV acceptance {n}"),
                    owner_account_id=account_id, make_default=True)
            await ProfileService().get_preferences(session, profile.id)
            await session.flush()
            accounts.append({"profile": str(profile.id), "cookie":
                AccountSessionSigner(settings.secret_key.get_secret_value()).issue(
                    account.id, account.session_version)})
        await session.commit()
        print(json.dumps({"accounts": accounts, "cookie_name": settings.user_session_cookie_name}))
asyncio.run(main())
"""


async def verify_mcp(token, revision):
    async with (
        streamablehttp_client(BASE + "/mcp", headers={"Authorization": "Bearer " + token}) as (
            read,
            write,
            _,
        ),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        assert initialized.serverInfo.name == "[DEV] JobHunter"
        result = await session.call_tool("get_system_status", {})
        assert not result.isError, "MCP status tool failed"
        body = result.structuredContent
        if not body:
            body = json.loads(result.content[0].text)
        assert body["deployment"]["is_dev"] is True
        assert body["deployment"]["revision"] == revision


def identified(page, revision):
    assert page.title().startswith("[DEV] "), page.url
    banner = page.get_by_role("note", name="DEV версия")
    assert banner.is_visible(), page.url
    assert revision in banner.inner_text(), page.url
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), page.url


def main():
    connect()
    candidate = json.loads((STATE / "candidate.json").read_text())
    revision = candidate["revision"]
    creds = json.loads((STATE / "credentials.json").read_text())
    with httpx.Client(
        base_url=BASE, headers={"Authorization": "Bearer " + creds["api_token"]}, timeout=120
    ) as client:
        for url in ("/health", "/ready", "/api/v1/status", "/api/v1/sources", "/api/openapi.json"):
            response = client.get(url)
            response.raise_for_status()
            assert response.headers["x-jobhunter-environment"] == "development"
            assert response.headers["x-jobhunter-revision"] == revision
        status = client.get("/api/v1/status").json()
        assert status["deployment"]["is_dev"] is True
        assert status["real_email_delivery_enabled"] is False
        assert client.get("/api/openapi.json").json()["info"]["title"] == "[DEV] JobHunter"
        sources = client.get("/api/v1/sources").json()
        fixture = next(source for source in sources if source["adapter_type"] == "fixture_source")
        for source in sources:
            if source is not fixture:
                assert not source["enabled"] and source["automatic_actions_paused"], source["name"]
        scan = client.post(f"/api/v1/sources/{fixture['id']}/scans/full")
        scan.raise_for_status()
        scan_id = scan.json()["scan_id"]
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/scans/{scan_id}")
            response.raise_for_status()
            run = response.json()
            if run["status"] in {"succeeded", "partial", "failed", "cancelled"}:
                assert run["status"] == "succeeded", run
                break
            time.sleep(10)
        else:
            raise RuntimeError("Actual A51 worker did not complete the fixture scan")
        jobs = client.get("/api/v1/jobs").json()
        assert jobs, "Fixture scan produced no jobs"
        print("A51 acceptance: API/catalog and actual queued fixture scan passed", flush=True)

    output = compose("exec -T api python -", data=PREPARE_ACCOUNTS.encode(), capture=True)
    accounts = json.loads(output.stdout.decode().splitlines()[-1])
    screenshots = STATE / "screenshots"
    screenshots.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for attempt in range(1, 4):
            context = browser.new_context(viewport={"width": 1440, "height": 960})
            page = context.new_page()
            page.set_default_timeout(120000)
            response = page.goto(BASE + "/admin")
            assert response and response.ok and "/login" in page.url
            identified(page, revision)
            page.get_by_text("Войти паролем администратора", exact=True).click()
            page.locator("input[name=password]").fill(creds["admin_password"])
            page.locator("form[action='/admin/login'] button[type=submit]").click()
            page.wait_for_url("**/admin**")
            for view in ("overview", "settings", "decisions", "history", "calls"):
                response = page.goto(BASE + "/admin?view=" + view)
                assert response and response.ok
                identified(page, revision)
                if view == "settings":
                    for source in sources:
                        assert source["name"] in page.locator("#sources").inner_text()
                    page.screenshot(path=str(screenshots / f"admin-{attempt}.png"), full_page=True)
            context.close()
            # Separate, clean user context on every repetition.
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.add_cookies(
                [
                    {
                        "name": accounts["cookie_name"],
                        "value": accounts["accounts"][0]["cookie"],
                        "url": BASE,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            page = context.new_page()
            page.set_default_timeout(120000)
            for view in ("overview", "settings", "decisions", "history"):
                response = page.goto(BASE + "/app?view=" + view)
                assert response and response.ok
                identified(page, revision)
                if view == "settings":
                    for source in sources:
                        assert source["name"] in page.locator("#sources").inner_text()
                    page.screenshot(path=str(screenshots / f"user-{attempt}.png"), full_page=True)
            foreign = accounts["accounts"][1]["profile"]
            response = page.goto(BASE + "/app?view=settings&profile_id=" + foreign)
            assert response and response.status == 404, "User could access another owner's profile"
            context.close()
            print(
                f"A51 acceptance: clean admin/user browser iteration {attempt}/3 passed", flush=True
            )
        browser.close()
    asyncio.run(verify_mcp(creds["api_token"], revision))
    (STATE / "browser-evidence.json").write_text(
        json.dumps(
            {
                "revision": revision,
                "consecutive_browser_iterations": 3,
                "fixture_scan_id": scan_id,
                "sources": sources,
                "oauth_provider": "not contacted; DEV sessions exercise ownership",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
