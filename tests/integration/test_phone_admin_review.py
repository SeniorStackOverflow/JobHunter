from __future__ import annotations

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
    PhoneVerificationStatus,
    TurnSpeaker,
)
from app.security.auth import hash_password
from app.settings import Settings

ADMIN_PASSWORD = "correct horse battery staple"


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
