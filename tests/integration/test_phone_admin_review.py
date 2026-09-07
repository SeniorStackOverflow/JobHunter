from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr
from selectolax.parser import HTMLParser
from sqlalchemy import select

from app.admin import router as admin_router
from app.admin import routes as admin_routes
from app.database import get_session
from app.models.entities import CallFact, CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import (
    CallFactState,
    CommunicationChannel,
    CommunicationDirection,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnSpeaker,
)
from app.security.auth import hash_password
from app.settings import Settings

ADMIN_PASSWORD = "correct horse battery staple"
_PLAYWRIGHT_AVAILABLE = importlib.util.find_spec("playwright") is not None


def _settings(tmp_path: Any) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://127.0.0.1:6379/15",
        public_base_url="http://testserver",
        secret_key=SecretStr("phone-admin-review-test-secret-at-least-32-characters"),
        admin_username="operator",
        admin_password_hash=SecretStr(hash_password(ADMIN_PASSWORD)),
        resume_storage_path=tmp_path / "resumes",
        email_provider="fake",
        real_email_delivery_enabled=False,
        llm_provider="mock",
    )


@pytest_asyncio.fixture
async def review_context(
    sqlite_session_factory: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    settings = _settings(tmp_path)
    monkeypatch.setattr(admin_routes, "get_settings", lambda: settings)
    monkeypatch.setattr("app.admin.phone_routes.get_settings", lambda: settings)
    application = FastAPI()
    application.include_router(admin_router)

    async def override_session():
        async with sqlite_session_factory() as db:
            yield db

    application.dependency_overrides[get_session] = override_session
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="review", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address="+37360123456",
            started_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            verification_status=PhoneVerificationStatus.NEEDS_REVIEW,
        )
        sms = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.SMS,
            transport="phonegate",
            transport_external_id="sms-review-1",
            direction=CommunicationDirection.INBOUND,
            remote_address="+37360123456",
            started_at=datetime(2026, 9, 1, 13, tzinfo=UTC),
        )
        db.add_all([call, sms])
        await db.flush()
        fact = CallFact(
            session_id=call.id,
            field="interview_date",
            raw_expression="завтра",
            normalized_value="2026-09-02",
            state=CallFactState.CANDIDATE,
        )
        db.add(fact)
        db.add(
            CallFact(
                session_id=call.id,
                field="interview_time",
                raw_expression="в два часа",
                normalized_value="14:00",
                state=CallFactState.CANDIDATE,
            )
        )
        db.add(
            CommunicationTurn(
                session_id=sms.id,
                seq=1,
                speaker=TurnSpeaker.EMPLOYER,
                text="Подтверждаю дату 2 сентября",
                occurred_at=datetime(2026, 9, 1, 13, tzinfo=UTC),
            )
        )
        await db.commit()
        call_id, sms_id = call.id, sms.id

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        login = await client.get("/login")
        token = HTMLParser(login.text).css_first("input[name='csrf_token']").attributes["value"]
        response = await client.post(
            "/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
        )
        assert response.status_code == 303
        yield client, call_id, sms_id, sqlite_session_factory


@pytest.mark.asyncio
async def test_fact_review_requires_csrf_and_confirms_manual_value(review_context) -> None:
    client, call_id, _sms_id, factory = review_context
    invalid = await client.post(
        f"/admin/phone/calls/{call_id}/facts/interview_date/review",
        data={"action": "correct", "value": "03.09.2026", "csrf_token": "bad"},
    )
    assert invalid.status_code == 403
    page = await client.get(f"/?view=calls&tab=history&session={call_id}")
    csrf = HTMLParser(page.text).css_first("input[name='csrf_token']").attributes["value"]
    response = await client.post(
        f"/admin/phone/calls/{call_id}/facts/interview_date/review",
        data={"action": "correct", "value": "03.09.2026", "csrf_token": csrf},
    )
    assert response.status_code == 303
    async with factory() as db:
        fact = await db.scalar(select(CallFact).where(CallFact.session_id == call_id))
        assert fact is not None
        assert fact.state is CallFactState.CONFIRMED
        assert fact.confirmation_source.value == "manual"
        assert fact.normalized_value == "2026-09-03"
        async with factory() as db:
            call = await db.get(CommunicationSession, call_id)
            assert call is not None
            assert call.verification_status is PhoneVerificationStatus.NEEDS_REVIEW
        page = await client.get(f"/?view=calls&tab=history&session={call_id}")
        csrf = HTMLParser(page.text).css_first("input[name='csrf_token']").attributes["value"]
        response = await client.post(
            f"/admin/phone/calls/{call_id}/facts/interview_time/review",
            data={"action": "confirm", "value": "14:00", "csrf_token": csrf},
        )
        assert response.status_code == 303
        async with factory() as db:
            call = await db.get(CommunicationSession, call_id)
            assert call is not None
            assert call.verification_status is PhoneVerificationStatus.CONFIRMED


@pytest.mark.asyncio
async def test_sms_link_and_unlink_require_matching_identity(review_context) -> None:
    client, call_id, sms_id, factory = review_context
    page = await client.get(f"/?view=calls&tab=history&session={call_id}")
    csrf = HTMLParser(page.text).css_first("input[name='csrf_token']").attributes["value"]
    link = await client.post(
        f"/admin/phone/calls/{call_id}/sms/{sms_id}/link", data={"csrf_token": csrf}
    )
    assert link.status_code == 303
    async with factory() as db:
        sms = await db.get(CommunicationSession, sms_id)
        assert sms is not None
        assert sms.related_session_id == call_id
    page = await client.get(f"/?view=calls&tab=history&session={call_id}")
    csrf = HTMLParser(page.text).css_first("input[name='csrf_token']").attributes["value"]
    unlink = await client.post(
        f"/admin/phone/calls/{call_id}/sms/{sms_id}/unlink", data={"csrf_token": csrf}
    )
    assert unlink.status_code == 303


@pytest.mark.asyncio
async def test_fact_review_rejects_active_processing_claim(review_context) -> None:
    client, call_id, _sms_id, factory = review_context
    async with factory() as db:
        call = await db.get(CommunicationSession, call_id)
        assert call is not None
        call.claim_token = "live-worker-claim"
        await db.commit()
    page = await client.get(f"/?view=calls&tab=history&session={call_id}")
    csrf = HTMLParser(page.text).css_first("input[name='csrf_token']").attributes["value"]
    response = await client.post(
        f"/admin/phone/calls/{call_id}/facts/interview_date/review",
        data={"action": "confirm", "value": "03.09.2026", "csrf_token": csrf},
    )
    assert response.status_code == 409
    async with factory() as db:
        fact = await db.scalar(select(CallFact).where(CallFact.session_id == call_id))
        assert fact is not None
        assert fact.state is CallFactState.CANDIDATE


@pytest.mark.asyncio
async def test_sms_link_rejects_multiple_or_non_employer_turns(review_context) -> None:
    client, call_id, sms_id, factory = review_context
    async with factory() as db:
        sms = await db.get(CommunicationSession, sms_id)
        assert sms is not None
        db.add(
            CommunicationTurn(
                session_id=sms.id,
                seq=2,
                speaker=TurnSpeaker.EMPLOYER,
                text="ещё одна реплика",
                occurred_at=datetime(2026, 9, 1, 13, 1, tzinfo=UTC),
            )
        )
        await db.commit()
    page = await client.get(f"/?view=calls&tab=history&session={call_id}")
    csrf = HTMLParser(page.text).css_first("input[name='csrf_token']").attributes["value"]
    response = await client.post(
        f"/admin/phone/calls/{call_id}/sms/{sms_id}/link", data={"csrf_token": csrf}
    )
    assert response.status_code == 404
    async with factory() as db:
        sms = await db.get(CommunicationSession, sms_id)
        assert sms is not None
        assert sms.related_session_id is None


@pytest.mark.skipif(
    os.getenv("RUN_PLAYWRIGHT_TESTS") != "1" or not _PLAYWRIGHT_AVAILABLE,
    reason=(
        "set RUN_PLAYWRIGHT_TESTS=1 and install Playwright to run the opt-in admin browser workflow"
    ),
)
@pytest.mark.asyncio
async def test_admin_review_playwright_narrow_view_and_state_panels(review_context) -> None:
    """Exercise the rendered review workflow in a real browser when opt-in."""
    if importlib.util.find_spec("playwright.async_api") is None:
        pytest.skip("Playwright is not installed; install browser test dependencies")
    from playwright import async_api as playwright_api

    client, call_id, _sms_id, factory = review_context
    response = await client.get(f"/?view=calls&tab=history&session={call_id}")
    assert response.status_code == 200
    async with playwright_api.async_playwright() as runtime:
        browser = await runtime.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(response.text, wait_until="domcontentloaded")
        assert await page.get_by_text("Проверка фактов").count() == 1
        assert await page.get_by_text("Нужна проверка").count() >= 1
        assert await page.get_by_text("Аудиодоказательство отсутствует").count() >= 1
        assert await page.get_by_text("Связанных SMS нет").count() == 1
        assert await page.locator("form[action*='/facts/interview_date/review']").count() >= 1
        has_horizontal_overflow = await page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        assert has_horizontal_overflow is False

        async with factory() as db:
            call = await db.get(CommunicationSession, call_id)
            assert call is not None
            fact = await db.scalar(select(CallFact).where(CallFact.session_id == call_id))
            assert fact is not None
            fact.state = CallFactState.CONFLICT
            call.summary_state = PhoneSummaryState.FAILED
            call.summary = {
                "verification": {
                    "sms_comparisons": [
                        {
                            "status": "needs_review",
                            "comparisons": [
                                {
                                    "field": "interview_date",
                                    "relation": "ambiguous",
                                    "reason_code": "review_required",
                                }
                            ],
                            "reason_codes": ["sms:interview_date:ambiguous"],
                        }
                    ]
                }
            }
            await db.commit()
        failed = await client.get(f"/?view=calls&tab=history&session={call_id}")
        await page.set_content(failed.text, wait_until="domcontentloaded")
        assert await page.get_by_text("резюме: ошибка").count() == 1
        assert await page.get_by_text("Нужна проверка: конфликт").count() == 1
        assert await page.get_by_text("ambiguous").count() >= 1

        async with factory() as db:
            facts = list(
                (await db.scalars(select(CallFact).where(CallFact.session_id == call_id))).all()
            )
            for fact in facts:
                await db.delete(fact)
            call = await db.get(CommunicationSession, call_id)
            assert call is not None
            call.summary = {}
            await db.commit()
        empty = await client.get(f"/?view=calls&tab=history&session={call_id}")
        await page.set_content(empty.text, wait_until="domcontentloaded")
        assert await page.get_by_text("Факты ещё не извлечены. Требуется проверка.").count() == 1
        await browser.close()
