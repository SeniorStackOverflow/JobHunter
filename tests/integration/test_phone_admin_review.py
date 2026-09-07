from __future__ import annotations

import asyncio
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

from app.admin import phone_routes
from app.admin import router as admin_router
from app.admin import routes as admin_routes
from app.database import get_session
from app.models.entities import (
    AuditEvent,
    CallFact,
    CommunicationSession,
    CommunicationTurn,
    UserProfile,
)
from app.models.enums import (
    CallFactConfirmationSource,
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
async def test_unknown_review_creates_missing_fact_and_audits(review_context) -> None:
    client, call_id, _sms_id, factory = review_context
    page = await client.get(f"/?view=calls&tab=history&session={call_id}")
    csrf = HTMLParser(page.text).css_first("input[name='csrf_token']").attributes["value"]
    response = await client.post(
        f"/admin/phone/calls/{call_id}/facts/timezone/review",
        data={"action": "unknown", "csrf_token": csrf},
    )
    assert response.status_code == 303
    async with factory() as db:
        fact = await db.scalar(
            select(CallFact).where(
                CallFact.session_id == call_id,
                CallFact.field == "timezone",
            )
        )
        call = await db.get(CommunicationSession, call_id)
        audit = await db.scalar(
            select(AuditEvent).where(
                AuditEvent.entity_id == str(call_id),
                AuditEvent.action == "phone.fact.unknown",
            )
        )
        assert fact is not None
        assert fact.state is CallFactState.UNKNOWN
        assert fact.raw_expression == "не указано"
        assert call is not None and call.verification_revision == 1
        assert audit is not None
        assert audit.sanitized_details["new_normalized_value"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["onsite", "remote", "phone"])
async def test_fact_review_accepts_canonical_interview_format(review_context, value: str) -> None:
    client, call_id, _sms_id, factory = review_context
    async with factory() as db:
        db.add(
            CallFact(
                session_id=call_id,
                field="format",
                raw_expression="формат не определён",
                state=CallFactState.UNKNOWN,
            )
        )
        await db.commit()
    page = await client.get(f"/?view=calls&tab=history&session={call_id}")
    document = HTMLParser(page.text)
    csrf = document.css_first("input[name='csrf_token']").attributes["value"]
    assert document.css_first("select[name='value']") is not None

    response = await client.post(
        f"/admin/phone/calls/{call_id}/facts/format/review",
        data={"action": "correct", "value": value, "csrf_token": csrf},
    )

    assert response.status_code == 303
    async with factory() as db:
        fact = await db.scalar(
            select(CallFact).where(CallFact.session_id == call_id, CallFact.field == "format")
        )
        assert fact is not None
        assert fact.normalized_value == value
        assert fact.state is CallFactState.CONFIRMED


async def _force_stale_revision(db: Any, call_id: Any) -> None:
    from sqlalchemy import update

    await db.execute(
        update(CommunicationSession)
        .where(CommunicationSession.id == call_id)
        .values(verification_revision=CommunicationSession.verification_revision + 1)
        .execution_options(synchronize_session=False)
    )


@pytest.mark.asyncio
async def test_fact_review_sqlite_cas_loss_rolls_back_fact_and_audit(
    review_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, call_id, _sms_id, factory = review_context
    original = phone_routes._reserve_call_mutation

    async def contend(db: Any, call: Any) -> Any:
        await _force_stale_revision(db, call_id)
        return await original(db, call)

    monkeypatch.setattr(phone_routes, "_reserve_call_mutation", contend)
    page = await client.get(f"/?view=calls&tab=history&session={call_id}")
    csrf = HTMLParser(page.text).css_first("input[name='csrf_token']").attributes["value"]
    response = await client.post(
        f"/admin/phone/calls/{call_id}/facts/interview_date/review",
        data={"action": "correct", "value": "03.09.2026", "csrf_token": csrf},
    )
    assert response.status_code == 409
    async with factory() as db:
        fact = await db.scalar(select(CallFact).where(CallFact.session_id == call_id))
        call = await db.get(CommunicationSession, call_id)
        audits = list(
            (
                await db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == str(call_id),
                        AuditEvent.action == "phone.fact.corrected",
                    )
                )
            ).all()
        )
        assert fact is not None and fact.state is CallFactState.CANDIDATE
        assert fact.normalized_value == "2026-09-02"
        assert call is not None and call.verification_revision == 0
        assert audits == []


@pytest.mark.asyncio
async def test_sms_link_sqlite_cas_loss_does_not_persist_relation_or_audit(
    review_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, call_id, sms_id, factory = review_context
    original = phone_routes._reserve_call_mutation

    async def contend(db: Any, call: Any) -> Any:
        await _force_stale_revision(db, call_id)
        return await original(db, call)

    monkeypatch.setattr(phone_routes, "_reserve_call_mutation", contend)
    page = await client.get(f"/?view=calls&tab=history&session={call_id}")
    csrf = HTMLParser(page.text).css_first("input[name='csrf_token']").attributes["value"]
    response = await client.post(
        f"/admin/phone/calls/{call_id}/sms/{sms_id}/link",
        data={"csrf_token": csrf},
    )
    assert response.status_code == 409
    async with factory() as db:
        sms = await db.get(CommunicationSession, sms_id)
        call = await db.get(CommunicationSession, call_id)
        audit = await db.scalar(
            select(AuditEvent).where(
                AuditEvent.entity_id == str(call_id),
                AuditEvent.action == "phone.sms.linked",
            )
        )
        assert sms is not None and sms.related_session_id is None
        assert call is not None and call.verification_revision == 0
        assert audit is None


@pytest.mark.asyncio
async def test_concurrent_sms_link_has_exactly_one_call_owner(
    review_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, first_call_id, sms_id, factory = review_context
    async with factory() as db:
        first = await db.get(CommunicationSession, first_call_id)
        assert first is not None
        second = CommunicationSession(
            profile_id=first.profile_id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address=first.remote_address,
            started_at=first.started_at,
            verification_status=PhoneVerificationStatus.NEEDS_REVIEW,
        )
        db.add(second)
        await db.commit()
        second_call_id = second.id

    original = phone_routes._get_sms_for_call
    both_loaded = asyncio.Event()
    loaded = 0

    async def synchronize_after_read(db: Any, call: Any, requested_sms_id: Any) -> Any:
        nonlocal loaded
        sms = await original(db, call, requested_sms_id)
        loaded += 1
        if loaded == 2:
            both_loaded.set()
        await asyncio.wait_for(both_loaded.wait(), timeout=2)
        return sms

    monkeypatch.setattr(phone_routes, "_get_sms_for_call", synchronize_after_read)
    page = await client.get(f"/?view=calls&tab=history&session={first_call_id}")
    csrf = HTMLParser(page.text).css_first("input[name='csrf_token']").attributes["value"]
    responses = await asyncio.gather(
        client.post(
            f"/admin/phone/calls/{first_call_id}/sms/{sms_id}/link",
            data={"csrf_token": csrf},
        ),
        client.post(
            f"/admin/phone/calls/{second_call_id}/sms/{sms_id}/link",
            data={"csrf_token": csrf},
        ),
    )

    assert sorted(response.status_code for response in responses) == [303, 409]
    async with factory() as db:
        sms = await db.get(CommunicationSession, sms_id)
        calls = [
            await db.get(CommunicationSession, first_call_id),
            await db.get(CommunicationSession, second_call_id),
        ]
        assert sms is not None and sms.related_session_id in {first_call_id, second_call_id}
        owners = [
            call
            for call in calls
            if call is not None
            and str(
                (call.summary or {})
                .get("verification", {})
                .get("sms_reconciliation", {})
                .get("sms_turn_id", "")
            )
        ]
        assert len(owners) == 1
        assert owners[0].id == sms.related_session_id


@pytest.mark.asyncio
async def test_history_and_detail_render_confirmation_provenance_and_loading_states(
    review_context,
) -> None:
    client, call_id, _sms_id, factory = review_context
    async with factory() as db:
        facts = list(
            (await db.scalars(select(CallFact).where(CallFact.session_id == call_id))).all()
        )
        facts[0].state = CallFactState.CONFIRMED
        facts[0].confirmation_source = CallFactConfirmationSource.SMS
        facts[1].state = CallFactState.CONFIRMED
        facts[1].confirmation_source = CallFactConfirmationSource.MANUAL
        call = await db.get(CommunicationSession, call_id)
        assert call is not None
        call.verification_status = PhoneVerificationStatus.CONFIRMED
        call.summary_state = PhoneSummaryState.PENDING
        await db.commit()

    history = await client.get("/?view=calls&tab=history")
    assert history.status_code == 200
    assert "Подтверждено вручную" in history.text

    detail = await client.get(f"/?view=calls&tab=history&session={call_id}")
    assert detail.status_code == 200
    assert "Подтверждено по SMS" in detail.text
    assert "Подтверждено вручную" in detail.text
    assert "Ожидает обработки" in detail.text
    assert "Обработка резюме ещё не началась." in detail.text

    async with factory() as db:
        call = await db.get(CommunicationSession, call_id)
        assert call is not None
        call.summary_state = PhoneSummaryState.PROCESSING
        await db.commit()
    processing = await client.get(f"/?view=calls&tab=history&session={call_id}")
    assert processing.status_code == 200
    assert "Обработка выполняется" in processing.text
    assert "Резюме формируется. Обновите страницу через минуту." in processing.text


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
    async with factory() as db:
        call = await db.get(CommunicationSession, call_id)
        assert call is not None
        call.summary_state = PhoneSummaryState.PENDING
        db.add(
            CallFact(
                session_id=call_id,
                field="format",
                raw_expression="формат не определён",
                state=CallFactState.UNKNOWN,
            )
        )
        await db.commit()
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
        assert await page.get_by_text("Ожидает обработки").count() >= 1
        assert await page.locator("form[action*='/facts/interview_date/review']").count() >= 1
        assert (
            await page.locator("form[action*='/facts/format/review'] select[name='value']").count()
            == 1
        )
        has_horizontal_overflow = await page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        assert has_horizontal_overflow is False

        async with factory() as db:
            call = await db.get(CommunicationSession, call_id)
            assert call is not None
            call.summary_state = PhoneSummaryState.PROCESSING
            await db.commit()
        processing = await client.get(f"/?view=calls&tab=history&session={call_id}")
        await page.set_content(processing.text, wait_until="domcontentloaded")
        assert await page.get_by_text("Обработка выполняется").count() >= 1

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
