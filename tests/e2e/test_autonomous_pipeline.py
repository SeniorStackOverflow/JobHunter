from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.applications import ApplicationService
from app.crawlers.pipeline import ScanService
from app.crawlers.registry import build_default_registry
from app.email.providers import FakeGmailProvider
from app.email.service import EmailService
from app.matching.providers import MockProvider
from app.matching.schemas import MatchResult
from app.matching.service import MatchingService
from app.models.entities import (
    Application,
    CanonicalJob,
    EmailDelivery,
    JobPreference,
    JobSource,
    MatchEvaluation,
    Resume,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    DeliveryStatus,
    JobStatus,
    MatchDecision,
    ScanType,
)
from app.settings import Settings


class FixtureFetcher:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def get(self, url: str, **_kwargs: object) -> httpx.Response:
        response = await self.client.get(url)
        if response.status_code == 410 and httpx.URL(url).path.endswith("/job/courier"):
            return httpx.Response(404, request=response.request)
        return response


def source_config(base: dict[str, Any], *, mirror: bool = False) -> dict[str, Any]:
    if not mirror:
        return base
    source = {**base["source"]}
    source["id"] = "fixture_mirror"
    source["locales"] = [{"code": "en", "start_urls": ["https://fixture-site/en/jobs?mirror=true"]}]
    source["discovery"] = {
        "category_pages": [],
        "region_pages": [],
        "additional_entrypoints": [],
    }
    return {"source": source}


def make_test_settings(resume_dir: Path) -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        email_provider="fake",
        real_email_delivery_enabled=False,
        resume_storage_path=resume_dir,
        llm_provider="mock",
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_autonomous_fixture_pipeline_is_safe_and_idempotent(
    fixture_site_client: httpx.AsyncClient,
    sqlite_session_factory,
    generic_source_configuration: dict[str, Any],
    tmp_path: Path,
) -> None:
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    resume_data = b"%PDF-1.7\nverified fixture CV"
    (resume_dir / "technical.pdf").write_bytes(resume_data)
    settings = make_test_settings(resume_dir)
    async with sqlite_session_factory() as session:
        profile = UserProfile(
            name="Fixture Candidate",
            location="Chisinau",
            languages=[
                {"code": "en", "confirmed": True},
                {"code": "ro", "confirmed": True},
            ],
            work_experience=[{"role": "Backend Developer", "confirmed": True}],
            skills=["Python", "PostgreSQL", "security"],
            confirmed_facts=[
                {
                    "id": "python",
                    "statement": "I have confirmed Python experience",
                    "keywords": ["python"],
                    "confirmed": True,
                }
            ],
        )
        preferences = JobPreference(
            allowed_categories=[
                "technology",
                "delivery",
                "hospitality",
                "warehouse",
                "logistics",
                "support",
                "assistant",
            ],
            auto_send_categories=[
                "technology",
                "delivery",
                "hospitality",
                "warehouse",
                "logistics",
                "support",
                "assistant",
            ],
            allowed_cities=["Chisinau", "Balti"],
            remote_allowed=True,
            willing_without_experience=True,
            consider_outside_primary_resume=True,
            maximum_daily_applications=20,
            minimum_auto_send_score=70,
            auto_send_enabled=True,
            global_pause=False,
        )
        resume = Resume(
            name="Technical CV",
            category="technology",
            storage_key="technical.pdf",
            original_filename="technical.pdf",
            mime_type="application/pdf",
            sha256=hashlib.sha256(resume_data).hexdigest(),
            active=True,
            verified=True,
            is_default=True,
        )
        primary_source = JobSource(
            name="Fixture Jobs",
            base_url="https://fixture-site",
            adapter_type="generic_html",
            configuration=source_config(generic_source_configuration),
            rate_limit=600,
            concurrency=2,
        )
        mirror_source = JobSource(
            name="Independent Mirror",
            base_url="https://fixture-site",
            adapter_type="generic_html",
            configuration=source_config(generic_source_configuration, mirror=True),
            rate_limit=600,
            concurrency=2,
        )
        session.add_all([profile, preferences, resume, primary_source, mirror_source])
        await session.commit()
        primary_source_id = primary_source.id
        mirror_source_id = mirror_source.id

    fetcher = FixtureFetcher(fixture_site_client)
    registry = build_default_registry(client_factory=lambda _source: fetcher)
    scanner = ScanService(sqlite_session_factory, registry)
    primary_run = await scanner.create_scan(primary_source_id, ScanType.FULL)
    primary_run = await scanner.run_scan(primary_run.id)
    mirror_run = await scanner.create_scan(mirror_source_id, ScanType.FULL)
    mirror_run = await scanner.run_scan(mirror_run.id)
    assert (primary_run.new_jobs, mirror_run.new_jobs) == (10, 1)

    async with sqlite_session_factory() as session:
        assert await session.scalar(select(func.count(SourceJob.id))) == 11
        assert await session.scalar(select(func.count(CanonicalJob.id))) == 10
        source_jobs = list(
            (
                await session.scalars(
                    select(SourceJob).where(SourceJob.source_id == primary_source_id)
                )
            ).all()
        )

    low_resume_high_preference = MatchResult(
        resume_fit=10,
        preference_fit=95,
        overall_fit=78,
        requirements_met=[],
        missing_requirements=[],
        risks=[],
        scam_indicators=[],
        decision=MatchDecision.AUTO_APPLY,
        reason="high explicit preference fit",
    )
    matcher = MatchingService(settings, MockProvider(low_resume_high_preference))
    async with sqlite_session_factory() as session:
        for job in source_jobs:
            await matcher.analyze(session, job.id)
        await session.commit()
        prompt_job = await session.scalar(
            select(SourceJob).where(SourceJob.external_job_id == "fx-007")
        )
        scam_job = await session.scalar(
            select(SourceJob).where(SourceJob.external_job_id == "fx-006")
        )
        assert prompt_job is not None and scam_job is not None
        prompt_match = await session.scalar(
            select(MatchEvaluation).where(MatchEvaluation.source_job_id == prompt_job.id)
        )
        scam_match = await session.scalar(
            select(MatchEvaluation).where(MatchEvaluation.source_job_id == scam_job.id)
        )
        assert prompt_match is not None and prompt_match.decision == MatchDecision.BLOCK
        assert scam_match is not None and scam_match.decision == MatchDecision.BLOCK

    application_service = ApplicationService(settings)
    async with sqlite_session_factory() as session:
        evaluations = list((await session.scalars(select(MatchEvaluation))).all())
        for canonical_id in dict.fromkeys(item.canonical_job_id for item in evaluations):
            await application_service.prepare(session, canonical_id)
        await session.commit()
        applications = list((await session.scalars(select(Application))).all())
        prompt_application = next(
            item for item in applications if item.source_job_id == prompt_job.id
        )
        scam_application = next(item for item in applications if item.source_job_id == scam_job.id)
        assert prompt_application.status == ApplicationStatus.BLOCKED
        assert scam_application.status == ApplicationStatus.BLOCKED
        auto_ids = [
            item.id for item in applications if item.status == ApplicationStatus.AUTO_APPROVED
        ]
        assert auto_ids, [
            (
                str(item.source_job_id),
                item.status.value,
                item.policy_result.get("rules_failed", []),
            )
            for item in applications
        ]

    fake_gmail = FakeGmailProvider()
    email_service = EmailService(settings, sqlite_session_factory, fake_gmail)
    for application_id in auto_ids:
        delivery = await email_service.send_application(application_id)
        assert delivery.status == DeliveryStatus.SENT
    sent_count = len(fake_gmail.outbox)
    assert sent_count == len(auto_ids)
    repeat = await email_service.send_application(auto_ids[0])
    assert repeat.status == DeliveryStatus.SENT
    assert len(fake_gmail.outbox) == sent_count

    await fixture_site_client.post("/__control__/phase/2")
    incremental = await scanner.create_scan(primary_source_id, ScanType.INCREMENTAL)
    incremental = await scanner.run_scan(incremental.id)
    assert incremental.new_jobs == 1
    assert incremental.updated_jobs == 1

    async with sqlite_session_factory() as session:
        new_job = await session.scalar(
            select(SourceJob).where(SourceJob.external_job_id == "fx-012")
        )
        assert new_job is not None
        await matcher.analyze(session, new_job.id)
        await application_service.prepare(session, new_job.canonical_job_id)
        await session.commit()
        assert await session.scalar(select(func.count(Application.id))) == len(applications) + 1
        assert await session.scalar(select(func.count(EmailDelivery.id))) == sent_count

    await fixture_site_client.post("/__control__/phase/3")
    first_recheck = await scanner.recheck_active_jobs(
        primary_source_id, close_after_confirmed_absence_count=2
    )
    second_recheck = await scanner.recheck_active_jobs(
        primary_source_id, close_after_confirmed_absence_count=2
    )
    assert first_recheck["possibly_closed"] == 1
    assert second_recheck["closed"] == 1
    async with sqlite_session_factory() as session:
        courier = await session.scalar(
            select(SourceJob).where(
                SourceJob.source_id == primary_source_id,
                SourceJob.external_job_id == "fx-002",
            )
        )
        assert courier is not None and courier.status == JobStatus.CLOSED
        courier_canonical = await session.get(CanonicalJob, courier.canonical_job_id)
        assert courier_canonical is not None
        assert courier_canonical.status == JobStatus.CLOSED
