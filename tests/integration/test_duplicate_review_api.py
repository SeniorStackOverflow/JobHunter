from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.api.dependencies import require_api_actor
from app.api.routes import router
from app.database import get_session
from app.deduplication import DeduplicationService
from app.models.entities import AuditEvent, SourceJob
from tests.unit.test_deduplication import job, source
from tests.unit.test_policy_and_email import make_graph


@pytest_asyncio.fixture
async def review_client(sqlite_session_factory):
    app = FastAPI()
    app.include_router(router)

    async def session_override():
        async with sqlite_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline"
    ) as client:
        # Authentication itself is unchanged; verify that both routes enforce it.
        assert (
            await client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000001/deduplication")
        ).status_code == 401
        assert (
            await client.post(
                "/api/v1/jobs/00000000-0000-0000-0000-000000000001/deduplication/review",
                json={"decision": "distinct", "reason": "operator review"},
            )
        ).status_code == 401
        app.dependency_overrides[require_api_actor] = lambda: "offline-reviewer"
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["distinct", "duplicate"])
async def test_review_records_explanation_and_persists_operator_choice(
    review_client, sqlite_session_factory, decision
):
    async with sqlite_session_factory() as session:
        src = source("review")
        session.add(src)
        await session.flush()
        first, second = (
            job(src.id, "a", "Warehouse Operator"),
            job(src.id, "b", "Warehouse Operator"),
        )
        first.description = second.description = None
        session.add_all([first, second])
        await session.flush()
        service = DeduplicationService()
        await service.assign(session, first)
        await service.assign(session, second)
        first_id, second_id = first.canonical_job_id, second.id
        await session.commit()
    response = await review_client.get(f"/api/v1/jobs/{second_id}/deduplication")
    assert response.status_code == 200
    assert response.json()["decision"]["status"] == "needs_review"
    response = await review_client.post(
        f"/api/v1/jobs/{second_id}/deduplication/review",
        json={
            "decision": decision,
            "target_canonical_job_id": str(first_id) if decision == "duplicate" else None,
            "reason": "Compared both publications and confirmed the relationship",
        },
    )
    assert response.status_code == 200, response.text
    confirmed = response.json()["canonical_job_id"]
    assert (confirmed == str(first_id)) is (decision == "duplicate")
    async with sqlite_session_factory() as session:
        second = await session.get(SourceJob, second_id)
        assert (
            str((await DeduplicationService().assign(session, second)).canonical_job.id)
            == confirmed
        )
        assert await session.scalar(
            select(AuditEvent.id).where(AuditEvent.action == "job.deduplication_reviewed")
        )


@pytest.mark.asyncio
async def test_review_cannot_override_explicitly_different_conditions(
    review_client, sqlite_session_factory
):
    async with sqlite_session_factory() as session:
        src = source("review")
        session.add(src)
        await session.flush()
        first, second = (
            job(src.id, "a", "Warehouse Operator"),
            job(src.id, "b", "Warehouse Operator"),
        )
        first.schedule, second.schedule = "day", "night"
        session.add_all([first, second])
        await session.flush()
        service = DeduplicationService()
        await service.assign(session, first)
        await service.assign(session, second)
        target, second_id = first.canonical_job_id, second.id
        original = second.canonical_job_id
        await session.commit()
    response = await review_client.post(
        f"/api/v1/jobs/{second_id}/deduplication/review",
        json={
            "decision": "duplicate",
            "target_canonical_job_id": str(target),
            "reason": "Manual request",
        },
    )
    assert response.status_code == 409
    async with sqlite_session_factory() as session:
        assert (await session.get(SourceJob, second_id)).canonical_job_id == original


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["distinct", "duplicate"])
async def test_review_preserves_existing_application_bindings(
    review_client, sqlite_session_factory, tmp_path, decision
):
    async with sqlite_session_factory() as session:
        values = await make_graph(session, tmp_path)
        item, application = values[5], values[-1]
        item_id, canonical_id = item.id, item.canonical_job_id
        application_id = application.id
        await session.commit()
    response = await review_client.post(
        f"/api/v1/jobs/{item_id}/deduplication/review",
        json={
            "decision": decision,
            "target_canonical_job_id": str(canonical_id),
            "reason": "Check immutable application history",
        },
    )
    assert response.status_code == 409
    async with sqlite_session_factory() as session:
        from app.models.entities import Application

        assert (await session.get(SourceJob, item_id)).canonical_job_id == canonical_id
        existing = await session.get(Application, application_id)
        assert existing.canonical_job_id == canonical_id and existing.source_job_id == item_id
