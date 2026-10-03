from __future__ import annotations

import hashlib
from html import escape
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.applications.service import ApplicationService
from app.crawlers.pipeline import ScanService
from app.crawlers.registry import build_default_registry
from app.deduplication.comparison import compare_jobs
from app.email.providers import FakeGmailProvider
from app.email.service import EmailService
from app.matching.providers import MockProvider
from app.matching.service import MatchingService
from app.models.entities import (
    Application,
    CanonicalJob,
    JobPreference,
    JobSource,
    Resume,
    SourceJob,
    UserProfile,
)
from app.models.enums import RunStatus, ScanType, SourceHealth
from app.settings import Settings
from tests.unit.test_delucru_adapter import BASE, FixtureFetcher, default_routes, fixture


@pytest.mark.asyncio
async def test_two_board_scans_match_and_send_one_application(
    sqlite_session_factory, tmp_path: Path
):
    """Actual HTML parsers, scans, matcher, policy, preparer and sender; all HTTP/email offline."""
    mirror = "https://mirror.example.test"
    category = "food-industry-horeca"
    listing = (
        '<html><section class="opening"><a class="opening-link" '
        'href="/job/mirror-55318">Посудомойщик</a></section></html>'
    )
    description = "Căutăm persoană la spălat vase pentru pizzerie."
    mirror_html = f'''<html><h1 class="role">Посудомойщик</h1>
    <a class="employer" href="{mirror}/company/casa">Casa della pizza SRL</a>
    <div class="job-copy">{escape(description)}</div><dd class="where">Кишинёв</dd>
    <dd class="hours">Full-time</dd><dd class="workplace">onsite</dd>
    <a class="apply-email">casadellapizzahr@gmail.com</a><a class="phone">+37360004589</a></html>'''
    routes = default_routes()
    # Isolate the actual Delucru vacancy from unrelated fixture listings.
    primary_listing = '<html><a href="/job/55318">Persoană la spălat vase</a></html>'
    routes[f"{BASE}/jobs"] = primary_listing
    routes[f"{BASE}/jobs/by-category"] = f'<html><a href="/jobs/{category}">{category}</a></html>'
    routes[f"{BASE}/jobs/{category}"] = primary_listing
    routes[f"{BASE}/jobs/by-city"] = fixture("empty_listing.html")
    routes[f"{BASE}/jobs/by-district"] = fixture("empty_listing.html")
    routes[f"{mirror}/jobs"] = listing
    routes[f"{mirror}/categories"] = (
        f'<html><a class="category" href="/jobs/{category}">{category}</a></html>'
    )
    routes[f"{mirror}/jobs/{category}"] = listing
    routes[f"{mirror}/job/mirror-55318"] = mirror_html
    fetcher = FixtureFetcher(routes)
    settings = Settings(
        _env_file=None,
        environment="test",
        llm_provider="mock",
        email_provider="fake",
        real_email_delivery_enabled=False,
        mail_routing_preflight_enabled=False,
        resume_storage_path=tmp_path,
    )
    pdf = b"%PDF-1.7\noffline verified CV"
    (tmp_path / "resume.pdf").write_bytes(pdf)
    async with sqlite_session_factory() as session:
        profile = UserProfile(
            id=uuid4(),
            name="Offline candidate",
            is_default=True,
            languages=[{"code": code, "confirmed": True} for code in ("ro", "ru")],
        )
        prefs = JobPreference(
            profile_id=profile.id,
            allowed_categories=[category],
            auto_send_categories=[category],
            willing_without_experience=True,
            consider_outside_primary_resume=True,
            maximum_daily_applications=10,
            minimum_auto_send_score=70,
            global_pause=False,
            auto_send_enabled=True,
        )
        resume = Resume(
            profile_id=profile.id,
            name="Verified CV",
            category=category,
            storage_key="resume.pdf",
            original_filename="resume.pdf",
            mime_type="application/pdf",
            sha256=hashlib.sha256(pdf).hexdigest(),
            verified=True,
            active=True,
            is_default=True,
        )
        primary = JobSource(
            name="Delucru offline",
            adapter_type="delucru_md",
            base_url=BASE,
            configuration={"live_mode": False, "locale_priority": ["ro"]},
            health_status=SourceHealth.HEALTHY,
            automatic_actions_paused=False,
        )
        secondary = JobSource(
            name="Independent board offline",
            adapter_type="generic_html",
            base_url=mirror,
            configuration={
                "source": {
                    "id": "offline_mirror",
                    "name": "Independent board",
                    "adapter": "generic_html",
                    "base_url": mirror,
                    "allowed_domains": ["mirror.example.test"],
                    "locales": [{"code": "ru", "start_urls": [f"{mirror}/jobs"]}],
                    "discovery": {"category_pages": [f"{mirror}/categories"]},
                    "selectors": {
                        "category_link": "a.category",
                        "listing_card": "section.opening",
                        "listing_link": "a.opening-link",
                        "title": "h1.role",
                        "company": "a.employer",
                        "employer_url": "a.employer::attr(href)",
                        "description": "div.job-copy",
                        "city": "dd.where",
                        "schedule": "dd.hours",
                        "workplace_type": "dd.workplace",
                        "email": "a.apply-email",
                        "phone": "a.phone",
                    },
                    "transforms": {"id_regex": r"/job/([^/?#]+)"},
                }
            },
            health_status=SourceHealth.HEALTHY,
            automatic_actions_paused=False,
        )
        session.add_all([profile, prefs, resume, primary, secondary])
        await session.commit()
        source_ids = [primary.id, secondary.id]
        profile_id = profile.id
    scanner = ScanService(
        sqlite_session_factory, build_default_registry(client_factory=lambda _: fetcher)
    )
    for source_id in source_ids:
        run = await scanner.run_scan((await scanner.create_scan(source_id, ScanType.FULL)).id)
        assert run.status is RunStatus.SUCCEEDED
    async with sqlite_session_factory() as session:
        jobs = list((await session.scalars(select(SourceJob))).all())
        assert len(jobs) == 2
        assert compare_jobs(jobs[0], jobs[1]).duplicate, [
            {
                field: getattr(job, field)
                for field in (
                    "title",
                    "company",
                    "employer_id",
                    "description",
                    "cities",
                    "schedule",
                    "employment_type",
                    "required_experience",
                    "no_experience",
                    "workplace_type",
                )
            }
            for job in jobs
        ]
        assert await session.scalar(select(func.count(CanonicalJob.id))) == 1
        assert jobs[0].employer_id == jobs[1].employer_id
        assert {job.public_email for job in jobs} == {"casadellapizzahr@gmail.com"}
        job_ids = [(job.id, job.canonical_job_id) for job in jobs]
    provider = FakeGmailProvider()
    sender = EmailService(settings, sqlite_session_factory, provider)
    application_ids = []
    for job_id, canonical_id in job_ids:
        async with sqlite_session_factory() as session:
            await MatchingService(settings, MockProvider()).analyze(session, job_id, profile_id)
            application = await ApplicationService(settings).prepare(
                session, canonical_id, profile_id
            )
            await session.commit()
            application_id = application.id
            application_ids.append(application_id)
        await sender.send_application(application_id)
    assert application_ids[0] == application_ids[1]
    assert len(provider.outbox) == 1
    async with sqlite_session_factory() as session:
        assert await session.scalar(select(func.count(Application.id))) == 1
