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
from app.models.entities import CanonicalJob, CommunicationSession, UserProfile
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    PhoneSummaryState,
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
    await session.commit()

    newest_first_ids = [str(rows[2].id), str(rows[1].id), str(rows[0].id)]
    return SeededCalls(db=session, newest_first_ids=newest_first_ids, profile_id=profile_id)


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


def test_calls_in_view_titles() -> None:
    from app.admin.routes import _VIEW_TITLES

    assert _VIEW_TITLES["calls"] == "Звонки"
