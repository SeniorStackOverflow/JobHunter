"""Run the minimum pipeline and its panel against an isolated local test server.

Use the repository venv; requires installed Playwright Chromium. Gmail and LLM
are test providers, and this harness never enables crawling or production I/O.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.settings import Settings, get_settings

Settings.model_config["env_file"] = None


async def scenario(base_url: str, work_dir: Path, artifacts_dir: Path | None = None) -> None:
    from datetime import UTC, datetime, timedelta

    import httpx
    from playwright.async_api import async_playwright

    from app.applications.service import prepare_pending_applications
    from app.database.base import Base
    from app.database.session import async_session_factory, engine
    from app.email.providers import FakeGmailProvider
    from app.email.service import EmailService, send_auto_approved_applications
    from app.employers import EmployerRelationshipService
    from app.matching.providers import MockProvider
    from app.matching.schemas import MatchResult
    from app.matching.service import MatchingService
    from app.models.entities import Application, EmailMailboxCursor
    from app.models.enums import (
        ApplicationStatus,
        EmployerInteractionChannel,
        EmployerInteractionType,
        MatchDecision,
    )
    from app.policies import PolicyEngine
    from tests.unit.test_daily_minimum import additional_candidate, apply_candidate
    from tests.unit.test_policy_and_email import make_graph

    class FixtureMatcher(MockProvider):
        async def evaluate(self, request):
            weak = request.company != "Example Company"
            return MatchResult(
                resume_fit=20 if weak else 90,
                preference_fit=65 if weak else 95,
                overall_fit=46 if weak else 92,
                requirements_met=[],
                missing_requirements=[],
                risks=[],
                scam_indicators=[],
                decision=MatchDecision.PREPARE_FOR_REVIEW if weak else MatchDecision.AUTO_APPLY,
                reason="Explicit fixture evidence",
                soft_mismatches=["resume_relevance"] if weak else [],
            )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for run in range(1, 4):
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.drop_all)
                    await connection.run_sync(Base.metadata.create_all)
                async with async_session_factory() as session:
                    graph = await make_graph(session, work_dir)
                    graph[1].is_default = True
                    preference = graph[2]
                    preference.additional_rules = {"minimum_daily_applications": 2}
                    preference.minimum_auto_send_score = 70
                    preference.maximum_daily_applications = 20
                    weaker = await additional_candidate(
                        session, graph, company="Second Company", score=46
                    )
                    excess = await additional_candidate(
                        session, graph, company="Third Company", score=46
                    )
                    source_ids = [graph[5].id, weaker[1].id, excess[1].id]
                    profile_id, excess_id = graph[1].id, excess[-1].id
                    await session.commit()
                context = await browser.new_context()
                await context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if route.request.url.startswith(base_url)
                        else route.abort()
                    ),
                )
                page = await context.new_page()
                try:
                    await page.goto(f"{base_url}/login")
                    await page.locator(".login-admin-fallback summary").click()
                    await page.locator("input[name=password]").fill("local-minimum-e2e-only")
                    await page.locator(".login-password-form button[type=submit]").click()
                    await page.wait_for_url("**/admin")
                    await page.goto(f"{base_url}/admin?view=settings&profile_id={profile_id}")
                    form = page.locator("form[action='/admin/preferences']")
                    await form.locator("input[name=minimum_daily_applications]").fill("2")
                    await form.locator("input[name=maximum_daily_applications]").fill("20")
                    await form.locator("button").click()
                    await page.wait_for_load_state("networkidle")

                    matcher = MatchingService(get_settings(), FixtureMatcher())
                    async with async_session_factory() as session:
                        for source_id in source_ids:
                            await matcher.analyze(session, source_id, profile_id)
                        await session.commit()
                    await prepare_pending_applications()
                    await page.goto(f"{base_url}/admin?view=overview&profile_id={profile_id}")
                    target = page.locator("[data-daily-target]")
                    assert "готовый резерв 2" in await target.inner_text()

                    provider = FakeGmailProvider()
                    real_service = EmailService

                    def sender(config, factory, service_class=real_service, test_provider=provider):
                        return service_class(config, factory, test_provider)

                    with patch("app.email.service.EmailService", side_effect=sender):
                        assert await send_auto_approved_applications() == 2
                    await prepare_pending_applications()
                    async with async_session_factory() as session:
                        excess_application = await session.get(Application, excess_id)
                        assert excess_application.status is not ApplicationStatus.AUTO_APPROVED
                    assert len(provider.outbox) == 2
                    assert len({message.recipient for message in provider.outbox}) == 2
                    await page.reload()
                    assert (
                        "Минимум выполнен" in await page.locator("[data-daily-target]").inner_text()
                    )
                    assert "обычный отбор" in await page.locator("[data-daily-target]").inner_text()
                    await page.goto(f"{base_url}/admin?view=history&profile_id={profile_id}")
                    history_text = await page.locator("body").inner_text()
                    if (
                        "Example Company" not in history_text
                        or "Second Company" not in history_text
                    ):
                        raise AssertionError(
                            f"History did not show the sent companies: {history_text}"
                        )
                    await page.screenshot(
                        path=str((artifacts_dir or work_dir) / f"minimum-run-{run}.png"),
                        full_page=True,
                    )
                    # Exercise the separate manual action through the real API,
                    # including authentication, database commit and cooldown.
                    async with async_session_factory() as session:
                        old = await additional_candidate(
                            session,
                            graph,
                            company="Old Unanswered Company",
                            score=95,
                            decision=MatchDecision.AUTO_APPLY,
                        )
                        await apply_candidate(session, PolicyEngine(get_settings()), graph, old)
                        old[-1].status = ApplicationStatus.SENT
                        old[-1].sent_at = datetime.now(UTC) - timedelta(days=46)
                        await EmployerRelationshipService().record_event(
                            session,
                            profile_id=profile_id,
                            employer_id=old[-1].employer_id,
                            application_id=old[-1].id,
                            event_type=EmployerInteractionType.APPLICATION_SENT,
                            channel=EmployerInteractionChannel.EMAIL,
                            idempotency_key=f"old-fixture-{run}",
                            occurred_at=old[-1].sent_at,
                        )
                        session.add(
                            EmailMailboxCursor(
                                account_id=graph[1].owner_account_id,
                                provider="gmail",
                                last_checked_at=datetime.now(UTC),
                            )
                        )
                        old_id, employer_id = old[-1].id, old[-1].employer_id
                        await session.commit()
                    async with httpx.AsyncClient(base_url=base_url) as client:
                        endpoint = f"/api/v1/employers/{employer_id}/close-unanswered"
                        payload = {
                            "profile_id": str(profile_id),
                            "reason": "Fixture thread checked",
                        }
                        assert (await client.post(endpoint, json=payload)).status_code == 401
                        response = await client.post(
                            endpoint,
                            json=payload,
                            headers={"Authorization": "Bearer local-minimum-e2e-api-only"},
                        )
                        assert response.status_code == 200, response.text
                        assert response.json()["cooldown_until"] is not None
                    async with async_session_factory() as session:
                        assert (
                            await session.get(Application, old_id)
                        ).status is ApplicationStatus.SENT
                    print(
                        f"PASS {run}/3: clean browser, settings, matching, preparation, "
                        "two companies, minimum, normal threshold and audited manual API",
                        flush=True,
                    )
                finally:
                    await context.close()
        finally:
            await browser.close()
            await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=9137)
    parser.add_argument("--artifacts-dir", type=Path)
    args = parser.parse_args()
    if args.serve:
        import uvicorn

        from app.security.auth import hash_password

        os.environ["ADMIN_PASSWORD_HASH"] = hash_password("local-minimum-e2e-only")
        get_settings.cache_clear()
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="warning")
        return
    with tempfile.TemporaryDirectory(prefix="jobhunter-minimum-e2e-") as temporary:
        work_dir = Path(temporary)
        base_url = f"http://127.0.0.1:{args.port}"
        # Explicit test settings keep a developer's credentials out of the child.
        os.environ.update(
            ENVIRONMENT="test",
            REAL_EMAIL_DELIVERY_ENABLED="false",
            EMAIL_PROVIDER="fake",
            LLM_PROVIDER="mock",
            DATABASE_URL=f"sqlite+aiosqlite:///{work_dir}/test.db",
            RESUME_STORAGE_PATH=str(work_dir),
            PUBLIC_BASE_URL=base_url,
            REDIS_URL="redis://127.0.0.1:55440/0",
            SECRET_KEY="local-minimum-e2e-secret-32-characters",  # noqa: S106 - isolated test key
        )
        from app.security.auth import hash_api_key

        os.environ["MCP_API_KEYS_HASHED"] = json.dumps([hash_api_key("local-minimum-e2e-api-only")])
        get_settings.cache_clear()
        from app.database.base import Base
        from app.database.session import engine

        async def initialize():
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            await engine.dispose()

        asyncio.run(initialize())
        with (work_dir / "server.log").open("w") as server_log:
            server = subprocess.Popen(  # noqa: S603 - fixed repository interpreter and module
                [
                    str(Path(".venv/bin/python").absolute()),
                    "-m",
                    "scripts.verify_daily_minimum_e2e",
                    "--serve",
                    "--port",
                    str(args.port),
                ],
                stdout=server_log,
                stderr=server_log,
                cwd=Path(__file__).resolve().parents[1],
            )
            try:
                import time

                import httpx

                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError((work_dir / "server.log").read_text())
                    try:
                        if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                            break
                    except httpx.RequestError:
                        pass
                    time.sleep(0.1)
                else:
                    raise TimeoutError("isolated test server did not become healthy")
                if args.artifacts_dir:
                    args.artifacts_dir.mkdir(parents=True, exist_ok=True)
                asyncio.run(scenario(base_url, work_dir, args.artifacts_dir))
            except BaseException:
                print((work_dir / "server.log").read_text()[-12000:])
                raise
            finally:
                server.terminate()
                server.wait(timeout=15)


if __name__ == "__main__":
    main()
