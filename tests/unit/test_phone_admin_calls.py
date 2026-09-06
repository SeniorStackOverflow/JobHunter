from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr
from selectolax.parser import HTMLParser

from app.admin import routes as admin_routes
from app.admin.phone_routes import build_calls_context
from app.database.session import get_session
from app.models.entities import (
    AuditEvent,
    CanonicalJob,
    CommunicationSession,
    CommunicationTurn,
    UserProfile,
)
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    PhoneSummaryState,
    TurnDeliveryStatus,
    TurnSpeaker,
)
from app.security.auth import hash_password
from app.settings import Settings
from tests.fixtures.fake_redis import FakeAsyncRedis

ADMIN_PASSWORD = "correct horse battery staple"


def _settings(tmp_path: Any) -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://127.0.0.1:6379/15",
        public_base_url="http://127.0.0.1:8000",
        secret_key=SecretStr("phone-calls-test-secret-key-at-least-32-characters"),
        admin_username="operator",
        admin_password_hash=SecretStr(hash_password(ADMIN_PASSWORD)),
        resume_storage_path=tmp_path / "resumes",
        email_provider="fake",
        real_email_delivery_enabled=False,
        llm_provider="mock",
    )


@dataclass
class SeededCalls:
    db: Any
    newest_first_ids: list[str]
    profile_id: UUID
    summarized_id: str


async def _seed(session: Any) -> SeededCalls:
    profile_id = uuid4()
    session.add(UserProfile(id=profile_id, is_default=True, name="Owner"))

    job = CanonicalJob(
        id=uuid4(),
        normalized_company="Example Corp",
        normalized_title="Backend Engineer",
        canonical_fingerprint=f"fp-{uuid4()}",
    )
    session.add(job)

    base = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    rows = [
        CommunicationSession(
            id=uuid4(),
            profile_id=profile_id,
            canonical_job_id=job.id if idx == 0 else None,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address="+37360123456",
            remote_raw="+37360123456",
            phonegate_event_id_start=idx,
            started_at=base + timedelta(hours=idx),
            ended_at=base + timedelta(hours=idx, minutes=3),
            outcome=CommunicationOutcome.COMPLETED,
            needs_review=(idx == 1),
            summary_state=PhoneSummaryState.DONE,
            summary={"hints": {"outcome_guess": "interview_proposed" if idx == 2 else "unknown"}},
        )
        for idx in range(3)
    ]
    # An SMS session must never appear in the call history.
    rows.append(
        CommunicationSession(
            id=uuid4(),
            profile_id=profile_id,
            channel=CommunicationChannel.SMS,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            remote_address="+37360999999",
            remote_raw="+37360999999",
            phonegate_event_id_start=99,
            started_at=base + timedelta(hours=5),
            summary_state=PhoneSummaryState.NOT_APPLICABLE,
        )
    )
    for row in rows:
        session.add(row)

    summarized = rows[0]
    summarized.script_stage = "wrap_up"
    summarized.diagnostics = {"asr_backend": "whisper"}
    summarized.rx_frame_stats = {"frames": 1234}
    summarized.summary = {
        "summary_text": "Работодатель предложил собеседование в четверг.",
        "hints": {"outcome_guess": "unknown"},
        "model_meta": {"model": "mock", "tokens": 42},
        "telegram": {"state": "sent"},
    }
    session.add(
        CommunicationTurn(
            id=uuid4(),
            session_id=summarized.id,
            phonegate_transcript_id=1,
            seq=0,
            speaker=TurnSpeaker.EMPLOYER,
            text="Здравствуйте, вы откликались на вакансию?",
            delivery_status=TurnDeliveryStatus.NOT_APPLICABLE,
            occurred_at=base,
        )
    )
    session.add(
        CommunicationTurn(
            id=uuid4(),
            session_id=summarized.id,
            phonegate_transcript_id=2,
            seq=1,
            speaker=TurnSpeaker.ASSISTANT,
            text="assistant raw",
            spoken_text="Да, добрый день.",
            delivery_status=TurnDeliveryStatus.DELIVERED,
            asr_confidence=0.91,
            audio_evidence_path="/evidence/2.wav",
            occurred_at=base + timedelta(seconds=5),
        )
    )
    session.add(
        AuditEvent(
            id=uuid4(),
            actor="operator",
            action="phone.call.summary.generated",
            entity_type="communication_session",
            entity_id=str(summarized.id),
            correlation_id="test-corr",
        )
    )
    await session.commit()

    newest_first_ids = [str(rows[2].id), str(rows[1].id), str(rows[0].id)]
    return SeededCalls(
        db=session,
        newest_first_ids=newest_first_ids,
        profile_id=profile_id,
        summarized_id=str(summarized.id),
    )


@pytest_asyncio.fixture
async def seeded_calls(sqlite_session_factory: Any) -> AsyncIterator[SeededCalls]:
    async with sqlite_session_factory() as session:
        yield await _seed(session)


@pytest_asyncio.fixture
async def admin_client(
    sqlite_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    settings = _settings(tmp_path)
    monkeypatch.setattr(admin_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(admin_routes, "_phone_redis", lambda: FakeAsyncRedis())

    application = FastAPI()
    application.include_router(admin_routes.router)

    async def override_session() -> AsyncIterator[Any]:
        async with sqlite_session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session

    async with sqlite_session_factory() as session:
        await _seed(session)

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        login_page = await client.get("/login")
        token = (
            HTMLParser(login_page.text).css_first("input[name='csrf_token']").attributes["value"]
        )
        logged_in = await client.post(
            "/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
        )
        assert logged_in.status_code == 303
        yield client


@pytest.mark.asyncio
async def test_calls_context_history_lists_sessions_newest_first(
    seeded_calls: SeededCalls,
) -> None:
    ctx = await build_calls_context(seeded_calls.db, tab="history", page=1, filter_="all", query="")
    assert [r["id"] for r in ctx["call_rows"]] == seeded_calls.newest_first_ids


@pytest.mark.asyncio
async def test_calls_context_filter_needs_review(seeded_calls: SeededCalls) -> None:
    ctx = await build_calls_context(
        seeded_calls.db, tab="history", page=1, filter_="needs_review", query=""
    )
    assert ctx["call_rows"]
    assert all(r["needs_review"] for r in ctx["call_rows"])


@pytest.mark.asyncio
async def test_calls_context_filter_interview_proposed_python_path(
    seeded_calls: SeededCalls,
) -> None:
    ctx = await build_calls_context(
        seeded_calls.db, tab="history", page=1, filter_="interview_proposed", query=""
    )
    assert [r["id"] for r in ctx["call_rows"]] == [seeded_calls.newest_first_ids[0]]


@pytest.mark.asyncio
async def test_calls_context_search_by_company(seeded_calls: SeededCalls) -> None:
    ctx = await build_calls_context(
        seeded_calls.db, tab="history", page=1, filter_="all", query="Example"
    )
    assert ctx["call_rows"]
    assert all("Example" in (r["company"] or "") for r in ctx["call_rows"])


@pytest.mark.asyncio
async def test_calls_context_masks_caller(seeded_calls: SeededCalls) -> None:
    ctx = await build_calls_context(seeded_calls.db, tab="history", page=1, filter_="all", query="")
    assert ctx["call_rows"]
    for row in ctx["call_rows"]:
        assert "123456" not in row["caller"]
        assert "•" in row["caller"]


@pytest.mark.asyncio
async def test_calls_context_live_tab_has_health(seeded_calls: SeededCalls) -> None:
    ctx = await build_calls_context(seeded_calls.db, tab="live", page=1, filter_="all", query="")
    assert ctx["tab"] == "live"
    assert "calls_health" in ctx
    assert ctx["call_rows"] == []


@pytest.mark.asyncio
async def test_calls_view_renders(admin_client: httpx.AsyncClient) -> None:
    resp = await admin_client.get("/?view=calls&tab=history")
    assert resp.status_code == 200
    assert "Звонки" in resp.text
    assert "123456" not in resp.text


@pytest.mark.asyncio
async def test_calls_nav_link_present_on_other_views(admin_client: httpx.AsyncClient) -> None:
    resp = await admin_client.get("/?view=overview")
    assert resp.status_code == 200
    assert "/?view=calls" in resp.text


@pytest.mark.asyncio
async def test_calls_detail_exposes_summary_and_evidence_url(
    seeded_calls: SeededCalls,
) -> None:
    ctx = await build_calls_context(
        seeded_calls.db,
        tab="history",
        page=1,
        filter_="all",
        query="",
        session_id=seeded_calls.summarized_id,
    )
    d = ctx["detail"]
    assert d["summary"]["summary_text"]
    assert d["summary_state"] == "done"
    assert d["session"]["script_stage"] == "wrap_up"
    assert d["session"]["rx_frame_stats"] == {"frames": 1234}
    assert [t["text"] for t in d["turns"]] == [
        "Здравствуйте, вы откликались на вакансию?",
        "Да, добрый день.",
    ]
    assert any(t["audio_evidence_url"] for t in d["turns"])
    assert d["turns"][1]["audio_evidence_url"] == (
        f"/admin/phone/evidence/{seeded_calls.summarized_id}/2.wav"
    )
    assert d["audit_events"][0]["action"] == "phone.call.summary.generated"


@pytest.mark.asyncio
async def test_calls_detail_absent_for_unknown_session(seeded_calls: SeededCalls) -> None:
    ctx = await build_calls_context(
        seeded_calls.db,
        tab="history",
        page=1,
        filter_="all",
        query="",
        session_id="not-a-uuid",
    )
    assert not ctx["detail"]
    ctx2 = await build_calls_context(
        seeded_calls.db,
        tab="history",
        page=1,
        filter_="all",
        query="",
        session_id=str(uuid4()),
    )
    assert not ctx2["detail"]


@pytest.mark.asyncio
async def test_calls_detail_page_renders(
    admin_client: httpx.AsyncClient, seeded_calls: SeededCalls
) -> None:
    resp = await admin_client.get(f"/?view=calls&tab=history&session={seeded_calls.summarized_id}")
    assert resp.status_code == 200
    assert "Итог звонка" in resp.text
    assert "Работодатель предложил собеседование в четверг." in resp.text
    assert "123456" not in resp.text


@pytest.mark.asyncio
async def test_calls_detail_page_non_numeric_page_no_500(
    admin_client: httpx.AsyncClient,
) -> None:
    resp = await admin_client.get("/?view=calls&tab=history&page=abc")
    assert resp.status_code != 500


def test_calls_in_view_titles() -> None:
    from app.admin.routes import _VIEW_TITLES

    assert _VIEW_TITLES["calls"] == "Звонки"
