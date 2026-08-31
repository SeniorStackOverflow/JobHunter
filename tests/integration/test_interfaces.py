from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr
from selectolax.parser import HTMLParser
from sqlalchemy import select

from app.admin import routes as admin_routes
from app.api import dependencies as api_dependencies
from app.api import routes as api_routes
from app.database.session import get_session
from app.learning import FEATURE_SCHEMA_VERSION
from app.matching.bindings import (
    confirmed_fact_hashes,
    preference_fingerprint,
    profile_fingerprint,
)
from app.matching.source_version import compute_source_matching_hash
from app.models.entities import (
    Alert,
    Application,
    AuditEvent,
    BatchScanRun,
    CanonicalJob,
    EmailDelivery,
    EmployerContact,
    JobPreference,
    JobSource,
    MatchEvaluation,
    Resume,
    ReviewFeedbackEvent,
    ReviewLearningSetting,
    ScanRun,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    ContactType,
    DeliveryStatus,
    JobStatus,
    MatchDecision,
    PolicyDecision,
    ReviewOutcome,
    ReviewReason,
    RunStatus,
    ScanType,
    SourceHealth,
    VerificationStatus,
)
from app.security.auth import SessionSigner, hash_api_key, hash_password
from app.settings import Settings

pytestmark = pytest.mark.integration

API_KEY = "interface-test-api-key"
ADMIN_PASSWORD = "correct horse battery staple"


def _settings(tmp_path: Any, *, production: bool = False) -> Settings:
    return Settings(
        environment="production" if production else "test",
        database_url=(
            "postgresql+asyncpg://job_agent:test@127.0.0.1/job_agent"
            if production
            else "sqlite+aiosqlite:///:memory:"
        ),
        redis_url="redis://127.0.0.1:6379/15",
        public_base_url="https://job-agent.example" if production else "http://127.0.0.1:8000",
        secret_key=SecretStr("interface-test-secret-key-at-least-32-characters"),
        admin_username="operator",
        admin_password_hash=SecretStr(hash_password(ADMIN_PASSWORD)),
        mcp_api_keys_hashed=[hash_api_key(API_KEY)],
        resume_storage_path=tmp_path / "resumes",
        email_provider="fake",
        real_email_delivery_enabled=False,
        llm_provider="openai" if production else "mock",
        openai_model="interface-test-model" if production else None,
        openai_api_key=(SecretStr("interface-test-openai-key") if production else None),
    )


@pytest_asyncio.fixture
async def interface_app(
    sqlite_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> AsyncIterator[tuple[FastAPI, Settings]]:
    settings = _settings(tmp_path, production=True)
    monkeypatch.setattr(api_dependencies, "get_settings", lambda: settings)
    monkeypatch.setattr(api_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(admin_routes, "get_settings", lambda: settings)

    application = FastAPI()
    application.include_router(api_routes.router)
    application.include_router(admin_routes.router)

    async def override_session() -> AsyncIterator[Any]:
        async with sqlite_session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    yield application, settings


def _csrf_token(html: str) -> str:
    element = HTMLParser(html).css_first("input[name='csrf_token']")
    assert element is not None
    value = element.attributes.get("value")
    assert value
    return value


async def _login_admin(
    client: httpx.AsyncClient,
    settings: Settings,
) -> str:
    login_page = await client.get("/login")
    assert login_page.status_code == 200
    logged_in = await client.post(
        "/login",
        data={
            "password": ADMIN_PASSWORD,
            "csrf_token": _csrf_token(login_page.text),
        },
    )
    assert logged_in.status_code == 303
    dashboard = await client.get("/")
    assert dashboard.status_code == 200
    return _csrf_token(dashboard.text)


async def _seed_review_application(
    session_factory: Any,
    settings: Settings,
    *,
    suffix: str,
    status: ApplicationStatus = ApplicationStatus.PENDING_REVIEW,
) -> dict[str, Any]:
    resume_bytes = b"%PDF-1.4\n% interface test resume\n%%EOF\n"
    storage_key = f"resume-{suffix}.pdf"
    settings.resume_storage_path.mkdir(parents=True, exist_ok=True)
    (settings.resume_storage_path / storage_key).write_bytes(resume_bytes)

    source = JobSource(
        name=f"Review source {suffix}",
        base_url="https://jobs.example.test",
        adapter_type="fixture_source",
        configuration={},
        enabled=True,
        health_status=SourceHealth.HEALTHY,
        automatic_actions_paused=False,
    )
    profile_id = uuid4()
    profile = UserProfile(
        id=profile_id,
        is_default=True,
        name="Interface Reviewer",
        contact_email="reviewer@example.test",
        languages=[{"code": "en", "confirmed": True}],
        confirmed_facts=[],
    )
    preferences = JobPreference(
        profile_id=profile_id,
        allowed_categories=["technology"],
        auto_send_categories=["technology"],
        auto_send_enabled=True,
        global_pause=False,
        maximum_daily_applications=10,
        minimum_auto_send_score=80,
    )
    resume = Resume(
        profile_id=profile_id,
        name=f"Verified resume {suffix}",
        category="technology",
        storage_key=storage_key,
        original_filename="verified-resume.pdf",
        mime_type="application/pdf",
        sha256=hashlib.sha256(resume_bytes).hexdigest(),
        active=True,
        verified=True,
        is_default=True,
    )
    canonical = CanonicalJob(
        normalized_company=f"example company {suffix}",
        normalized_title="backend engineer",
        normalized_location="chisinau",
        canonical_fingerprint=hashlib.sha256(f"canonical-{suffix}".encode()).hexdigest(),
        status=JobStatus.ACTIVE,
    )
    async with session_factory() as session:
        session.add_all([source, profile, preferences, resume, canonical])
        await session.flush()
        job = SourceJob(
            source_id=source.id,
            canonical_job_id=canonical.id,
            external_job_id=f"review-{suffix}",
            canonical_url=f"https://jobs.example.test/jobs/{suffix}",
            localized_urls={"en": f"https://jobs.example.test/jobs/{suffix}"},
            title="Backend Engineer",
            company=f"Example Company {suffix}",
            categories_seen=["technology"],
            category="technology",
            description="Build and maintain a Python service.",
            location="Chisinau",
            cities=["Chisinau"],
            public_email="jobs@example.test",
            page_locale="en",
            content_hash=hashlib.sha256(f"content-{suffix}".encode()).hexdigest(),
            source_fingerprint=hashlib.sha256(f"source-{suffix}".encode()).hexdigest(),
            status=JobStatus.ACTIVE,
            raw_metadata={},
        )
        job.matching_content_hash = compute_source_matching_hash(job)
        session.add(job)
        await session.flush()
        canonical.primary_source_job_id = job.id
        evaluation = MatchEvaluation(
            profile_id=profile_id,
            canonical_job_id=canonical.id,
            source_job_id=job.id,
            resume_fit=90,
            preference_fit=95,
            overall_fit=92,
            requirements_met=["Python"],
            missing_requirements=[],
            risks=[],
            scam_indicators=[],
            explanation="Validated fixture evaluation.",
            decision=MatchDecision.AUTO_APPLY,
            model="mock",
            prompt_rules_version="test-v1",
            source_content_hash=job.content_hash,
            source_matching_hash=job.matching_content_hash,
            resume_id=resume.id,
            resume_sha256=resume.sha256,
            profile_fingerprint=profile_fingerprint(profile),
            preference_fingerprint=preference_fingerprint(preferences),
            confirmed_fact_hashes=confirmed_fact_hashes(profile),
        )
        contact = EmployerContact(
            canonical_job_id=canonical.id,
            source_job_id=job.id,
            value="jobs@example.test",
            contact_type=ContactType.EMAIL,
            discovery_source="vacancy",
            official_domain="example.test",
            verification_status=VerificationStatus.VERIFIED,
            confidence=1.0,
            evidence_url=job.canonical_url,
        )
        session.add_all([evaluation, contact])
        await session.flush()
        application = Application(
            profile_id=profile_id,
            canonical_job_id=canonical.id,
            source_job_id=job.id,
            match_evaluation_id=evaluation.id,
            resume_id=resume.id,
            recipient_contact_id=contact.id,
            subject="Application for Backend Engineer",
            body=(
                f"Hello Example Company {suffix} team,\n\n"
                "I would like to apply for the Backend Engineer position.\n\n"
                "Kind regards,\nInterface Reviewer"
            ),
            language="en",
            status=status,
            policy_decision=PolicyDecision.PENDING_REVIEW,
            policy_result={
                "decision": "pending_review",
                "rules_passed": ["vacancy_active"],
                "rules_failed": ["manual_review_requested"],
                "policy_version": "test-v1",
            },
            used_confirmed_facts=[],
            content_validated=True,
            idempotency_key=hashlib.sha256(f"application-{suffix}".encode()).hexdigest(),
        )
        session.add(application)
        await session.commit()
        return {
            "application_id": application.id,
            "profile_id": profile.id,
            "contact_id": contact.id,
            "resume_id": resume.id,
            "source_id": source.id,
            "source_job_id": job.id,
            "canonical_job_id": canonical.id,
        }


async def test_rest_writes_require_bearer_auth_and_are_audited(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, _ = interface_app
    transport = httpx.ASGITransport(app=application)
    payload = {
        "name": "Interface Test User",
        "contact_email": "user@example.com",
        "skills": ["Python"],
    }
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        missing = await client.put("/api/v1/profile", json=payload)
        invalid = await client.put(
            "/api/v1/profile",
            json=payload,
            headers={"Authorization": "Bearer wrong-key"},
        )
        accepted = await client.put(
            "/api/v1/profile",
            json=payload,
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 403
    assert accepted.status_code == 200
    async with sqlite_session_factory() as session:
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "profile.updated")
        )
        assert audit is not None
        assert audit.actor == "api-key"


async def test_rest_preference_updates_preserve_hidden_fields_and_protect_auto_send(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, _ = interface_app
    async with sqlite_session_factory() as session:
        profile = UserProfile(id=uuid4(), is_default=True, name="Preference Owner")
        session.add(profile)
        session.add(
            JobPreference(
                profile_id=profile.id,
                allowed_categories=["technology"],
                minimum_salary=Decimal("1250.00"),
                salary_currency="EUR",
                allowed_schedules=["day"],
                forbidden_schedules=["night"],
                language_constraints=[{"code": "en", "minimum": "B2"}],
                additional_rules={"contract_required": True},
                auto_send_enabled=False,
                global_pause=True,
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=application)
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        protected = await client.put(
            "/api/v1/preferences",
            headers=headers,
            json={"auto_send_enabled": True, "global_pause": False},
        )
        updated = await client.put(
            "/api/v1/preferences",
            headers=headers,
            json={"allowed_categories": ["operations"]},
        )
        resumed = await client.post("/api/v1/preferences/resume", headers=headers)
        paused = await client.post("/api/v1/preferences/pause", headers=headers)

    assert protected.status_code == 422
    assert updated.status_code == 200
    assert resumed.json() == {"auto_send_enabled": True, "global_pause": False}
    assert paused.json() == {"auto_send_enabled": True, "global_pause": True}
    async with sqlite_session_factory() as session:
        preferences = await session.scalar(select(JobPreference))
        assert preferences is not None
        assert preferences.allowed_categories == ["operations"]
        assert preferences.minimum_salary == Decimal("1250.00")
        assert preferences.salary_currency == "EUR"
        assert preferences.allowed_schedules == ["day"]
        assert preferences.forbidden_schedules == ["night"]
        assert preferences.language_constraints == [{"code": "en", "minimum": "B2"}]
        assert preferences.additional_rules == {"contract_required": True}
        assert preferences.auto_send_enabled is True
        assert preferences.global_pause is True
        actions = set((await session.scalars(select(AuditEvent.action))).all())
        assert {"preferences.updated", "auto_send.resumed", "auto_send.paused"} <= actions


def _valid_generic_configuration() -> dict[str, Any]:
    return {
        "source": {
            "id": "interface-source",
            "name": "Interface source",
            "adapter": "generic_html",
            "base_url": "https://jobs.example.com",
            "allowed_domains": ["jobs.example.com"],
            "locales": [{"code": "en", "start_urls": ["https://jobs.example.com/jobs"]}],
            "selectors": {
                "listing_card": ".job",
                "listing_link": "a.job-link",
                "title": "h1",
            },
        }
    }


async def test_rest_rejects_malformed_adapter_configuration_before_commit(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, _ = interface_app
    stored_source = JobSource(
        name="Existing generic source",
        base_url="https://jobs.example.com",
        adapter_type="generic_html",
        configuration=_valid_generic_configuration(),
        health_status=SourceHealth.HEALTHY,
    )
    async with sqlite_session_factory() as session:
        session.add(stored_source)
        await session.commit()
        source_id = stored_source.id

    transport = httpx.ASGITransport(app=application)
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        malformed_add = await client.post(
            "/api/v1/sources",
            headers=headers,
            json={
                "name": "Malformed",
                "base_url": "https://jobs.invalid.example",
                "adapter_type": "generic_html",
                "configuration": {},
            },
        )
        unsafe_scheme = await client.post(
            "/api/v1/sources",
            headers=headers,
            json={
                "name": "Unsafe link",
                "base_url": "javascript:alert(1)",
                "adapter_type": "generic_html",
                "configuration": _valid_generic_configuration(),
            },
        )
        malformed_update = await client.patch(
            f"/api/v1/sources/{source_id}",
            headers=headers,
            json={"configuration": {}},
        )

    assert malformed_add.status_code == 422
    assert unsafe_scheme.status_code == 422
    assert malformed_update.status_code == 422
    async with sqlite_session_factory() as session:
        sources = list((await session.scalars(select(JobSource))).all())
        assert len(sources) == 1
        assert sources[0].configuration == _valid_generic_configuration()


async def test_admin_login_mobile_page_and_csrf_enforcement(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    async with sqlite_session_factory() as session:
        session.add(UserProfile(id=uuid4(), is_default=True, name="Admin Owner"))
        session.add(
            JobSource(
                name="Unsafe legacy source",
                base_url="javascript:alert(1)",
                adapter_type="fixture_source",
                configuration={},
                health_status=SourceHealth.UNKNOWN,
            )
        )
        session.add(
            Alert(
                severity="warning",
                code="adapter_degradation",
                message="Historical UI fixture",
                safe_diagnostics={"source": "fixture"},
                acknowledged=False,
                created_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
        await session.commit()
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        login_page = await client.get("/login")
        assert login_page.status_code == 200
        assert login_page.headers["cache-control"] == "no-store"
        assert 'name="viewport"' in login_page.text
        assert "Вход в панель управления" in login_page.text
        assert 'src="/admin-assets/admin.js?v=' in login_page.text
        assert "data-password-toggle" in login_page.text
        assert 'name="username"' not in login_page.text
        assert "Аварийный вход" not in login_page.text
        oauth_error_page = await client.get(
            "/login", params={"oauth_error": "admin_identity_not_allowed"}
        )
        assert "Этот Google-аккаунт не имеет доступа" in oauth_error_page.text
        login_csrf = _csrf_token(login_page.text)

        rejected = await client.post(
            "/login",
            data={
                "password": ADMIN_PASSWORD,
                "csrf_token": "invalid",
            },
        )
        assert rejected.status_code == 401

        logged_in = await client.post(
            "/login",
            data={
                "password": ADMIN_PASSWORD,
                "csrf_token": login_csrf,
            },
        )
        assert logged_in.status_code == 303
        set_cookie = logged_in.headers["set-cookie"].casefold()
        assert "httponly" in set_cookie
        assert "secure" in set_cookie
        assert "samesite=strict" in set_cookie
        already_authenticated = await client.get("/login")
        assert already_authenticated.status_code == 303
        assert already_authenticated.headers["location"] == "/"

        dashboard = await client.get("/")
        assert dashboard.status_code == 200
        assert dashboard.headers["cache-control"] == "no-store"
        assert 'name="viewport"' in dashboard.text
        assert "Unsafe legacy source" in dashboard.text
        assert "Требует вашего внимания" in dashboard.text
        assert "Google OAuth не настроен" in dashboard.text
        assert 'href="javascript:' not in dashboard.text.casefold()
        assert "data-confirm-dialog" in dashboard.text
        assert 'src="/admin-assets/admin.js?v=' in dashboard.text
        assert "data-profile-select" in dashboard.text
        assert " onchange=" not in dashboard.text.casefold()
        assert ".confirm-reason[hidden]{display:none}" in dashboard.text
        for view, heading in {
            "decisions": "Требуют решения",
            "history": "История",
            "settings": "Критерии и лимиты",
            "diagnostics": "Текущие проблемы",
        }.items():
            page = await client.get("/", params={"view": view})
            assert page.status_code == 200
            assert heading in page.text
            assert page.text.count('class="admin-section"') == 1
            if view == "diagnostics":
                assert len(HTMLParser(page.text).css(".archive-item")) == 1
                assert "Отметить просмотренным" in page.text
                assert "white-space:normal!important" in page.text
        feedback_page = await client.get("/", params={"notice": "preferences_saved"})
        assert "Настройки сохранены" in feedback_page.text
        assert "data-notice-dismiss" in feedback_page.text
        dashboard_csrf = _csrf_token(dashboard.text)

        wrong_binding = await client.post("/admin/pause/true", data={"csrf_token": login_csrf})
        assert wrong_binding.status_code == 403
        paused = await client.post("/admin/pause/true", data={"csrf_token": dashboard_csrf})
        assert paused.status_code == 303

    async with sqlite_session_factory() as session:
        preferences = await session.scalar(select(JobPreference))
        assert preferences is not None
        assert preferences.global_pause is True
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auto_send.paused")
        )
        assert audit is not None

    forged_subject = SessionSigner(settings.secret_key.get_secret_value()).issue("not-operator")
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={settings.session_cookie_name: forged_subject},
    ) as forged_client:
        assert (await forged_client.get("/")).status_code == 303


async def test_admin_forms_merge_unexposed_fields_and_require_explicit_resume(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    async with sqlite_session_factory() as session:
        profile_id = uuid4()
        session.add_all(
            [
                UserProfile(
                    id=profile_id,
                    is_default=True,
                    name="Before",
                    languages=[{"code": "ro", "confirmed": True}],
                    work_experience=[{"role": "Engineer", "confirmed": True}],
                    education=[{"school": "Verified University"}],
                    skills=["Python"],
                    driving_licences=["B"],
                    confirmed_facts=[{"id": "fact-1", "confirmed": True}],
                    availability={"notice_days": 14},
                ),
                JobPreference(
                    profile_id=profile_id,
                    minimum_salary=Decimal("1500.00"),
                    salary_currency="EUR",
                    allowed_schedules=["day"],
                    forbidden_schedules=["night"],
                    language_constraints=[{"code": "ro"}],
                    additional_rules={
                        "verified_only": True,
                        "minimum_daily_applications": 3,
                        "force_minimum_daily_applications": False,
                    },
                    auto_send_enabled=False,
                    global_pause=True,
                ),
            ]
        )
        await session.commit()

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        csrf_token = await _login_admin(client, settings)
        settings_page = await client.get("/?view=settings")
        assert settings_page.status_code == 200
        assert 'name="minimum_daily_applications" value="3"' in settings_page.text
        assert 'data-daily-minimum readonly aria-disabled="true"' in settings_page.text
        assert "Принудительный добор" in settings_page.text

        legacy_preferences_form = await client.post(
            "/admin/preferences",
            data={
                "maximum_daily_applications": "4",
                "minimum_auto_send_score": "88",
                "csrf_token": csrf_token,
            },
        )
        assert legacy_preferences_form.status_code == 303
        async with sqlite_session_factory() as session:
            legacy_safe_preferences = await session.scalar(select(JobPreference))
            assert legacy_safe_preferences is not None
            assert legacy_safe_preferences.additional_rules["minimum_daily_applications"] == 3
            assert legacy_safe_preferences.additional_rules["verified_only"] is True

        invalid_daily_range = await client.post(
            "/admin/preferences",
            data={
                "minimum_daily_applications": "5",
                "maximum_daily_applications": "4",
                "minimum_auto_send_score": "88",
                "force_minimum_daily_applications": "on",
                "daily_application_rules_present": "true",
                "csrf_token": csrf_token,
            },
        )
        assert invalid_daily_range.status_code == 422
        async with sqlite_session_factory() as session:
            unchanged_preferences = await session.scalar(select(JobPreference))
            assert unchanged_preferences is not None
            assert unchanged_preferences.additional_rules["minimum_daily_applications"] == 3

        dormant_daily_range = await client.post(
            "/admin/preferences",
            data={
                "minimum_daily_applications": "5",
                "maximum_daily_applications": "4",
                "minimum_auto_send_score": "88",
                "daily_application_rules_present": "true",
                "csrf_token": csrf_token,
            },
        )
        assert dormant_daily_range.status_code == 303
        async with sqlite_session_factory() as session:
            dormant_preferences = await session.scalar(select(JobPreference))
            assert dormant_preferences is not None
            assert dormant_preferences.maximum_daily_applications == 4
            assert dormant_preferences.additional_rules["minimum_daily_applications"] == 5
            assert dormant_preferences.additional_rules["force_minimum_daily_applications"] is False

        profile_saved = await client.post(
            "/admin/profile",
            data={
                "name": "After",
                "contact_email": "after@example.com",
                "phone": "+37300000000",
                "location": "Chisinau",
                "languages": "en, ro",
                "skills": "Python, SQL",
                "csrf_token": csrf_token,
            },
        )
        preferences_saved = await client.post(
            "/admin/preferences",
            data={
                "allowed_categories": "operations",
                "auto_send_categories": "operations",
                "minimum_daily_applications": "2",
                "maximum_daily_applications": "4",
                "minimum_auto_send_score": "88",
                "force_minimum_daily_applications": "on",
                "daily_application_rules_present": "true",
                "auto_send_enabled": "on",
                "global_pause": "false",
                "csrf_token": csrf_token,
            },
        )
        assert profile_saved.status_code == 303
        assert preferences_saved.status_code == 303
        resume_uploaded = await client.post(
            "/admin/resumes",
            data={
                "profile_id": str(profile_id),
                "name": "Admin upload",
                "category": "operations",
                "csrf_token": csrf_token,
            },
            files={"file": ("admin.pdf", b"%PDF-1.7\nadmin upload\n%%EOF", "application/pdf")},
        )
        assert resume_uploaded.status_code == 303

        async with sqlite_session_factory() as session:
            profile = await session.scalar(select(UserProfile))
            preferences = await session.scalar(select(JobPreference))
            assert profile is not None
            assert preferences is not None
            assert profile.name == "After"
            assert profile.work_experience == [{"role": "Engineer", "confirmed": True}]
            assert profile.education == [{"school": "Verified University"}]
            assert profile.driving_licences == ["B"]
            assert profile.confirmed_facts == [{"id": "fact-1", "confirmed": True}]
            assert profile.availability == {"notice_days": 14}
            assert preferences.allowed_categories == ["operations"]
            assert preferences.minimum_salary == Decimal("1500.00")
            assert preferences.salary_currency == "EUR"
            assert preferences.allowed_schedules == ["day"]
            assert preferences.forbidden_schedules == ["night"]
            assert preferences.language_constraints == [{"code": "ro"}]
            assert preferences.additional_rules == {
                "verified_only": True,
                "minimum_daily_applications": 2,
                "force_minimum_daily_applications": True,
            }
            assert preferences.auto_send_enabled is False
            assert preferences.global_pause is True
            preferences_audit = await session.scalar(
                select(AuditEvent)
                .where(AuditEvent.action == "preferences.updated")
                .order_by(AuditEvent.timestamp.desc())
            )
            assert preferences_audit is not None
            assert preferences_audit.sanitized_details["minimum_daily_applications"] == 2
            assert preferences_audit.sanitized_details["maximum_daily_applications"] == 4
            assert preferences_audit.sanitized_details["force_minimum_daily_applications"] is True
            uploaded_resume = await session.scalar(
                select(Resume).where(Resume.name == "Admin upload")
            )
            assert uploaded_resume is not None
            assert uploaded_resume.profile_id == profile_id

        resumed = await client.post(
            "/admin/pause/false",
            data={"csrf_token": csrf_token},
        )
        assert resumed.status_code == 303

    async with sqlite_session_factory() as session:
        preferences = await session.scalar(select(JobPreference))
        assert preferences is not None
        assert preferences.auto_send_enabled is True
        assert preferences.global_pause is False
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auto_send.resumed")
        )
        assert audit is not None
        assert audit.decision == "enabled_and_resumed"


async def test_admin_application_detail_approval_and_policy_gated_send(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.database.session as database_session

    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="admin-send",
    )
    application_id = seeded["application_id"]
    monkeypatch.setattr(database_session, "async_session_factory", sqlite_session_factory)

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        assert (await client.get(f"/admin/applications/{application_id}")).status_code == 303
        csrf_token = await _login_admin(client, settings)

        detail = await client.get(f"/admin/applications/{application_id}")
        assert detail.status_code == 200
        assert detail.headers["cache-control"] == "no-store"
        assert "Backend Engineer" in detail.text
        assert "Example Company admin-send" in detail.text
        assert "I would like to apply" in detail.text
        assert "Verified resume admin-send" in detail.text
        assert "jobs@example.test" in detail.text
        assert "https://jobs.example.test/jobs/admin-send" in detail.text
        assert "manual_review_requested" in detail.text
        assert "storage_key" not in detail.text
        assert "data-review-reject" in detail.text

        approved = await client.post(
            f"/admin/applications/{application_id}/approve",
            data={"csrf_token": csrf_token},
        )
        assert approved.status_code == 303

        # The secure session cookie was already issued; test mode permits only the fake
        # external provider while retaining the same policy and persisted-data path.
        settings.environment = "test"
        sent = await client.post(
            f"/admin/applications/{application_id}/send",
            data={"csrf_token": csrf_token},
        )
        assert sent.status_code == 303
        sent_detail = await client.get(f"/admin/applications/{application_id}")
        assert sent_detail.status_code == 200
        assert f"fake-{application_id}" in sent_detail.text
        assert f"thread-{application_id}" in sent_detail.text

    async with sqlite_session_factory() as session:
        stored = await session.get(Application, application_id)
        delivery = await session.scalar(
            select(EmailDelivery).where(EmailDelivery.application_id == application_id)
        )
        feedback = await session.scalar(
            select(ReviewFeedbackEvent).where(ReviewFeedbackEvent.application_id == application_id)
        )
        assert stored is not None
        assert delivery is not None
        assert stored.status == ApplicationStatus.SENT
        assert delivery.status == DeliveryStatus.SENT
        assert delivery.provider_message_id == f"fake-{application_id}"
        assert feedback is not None
        assert feedback.outcome == ReviewOutcome.APPROVED
        assert feedback.learning_eligible is True
        actions = set((await session.scalars(select(AuditEvent.action))).all())
        assert {"application.approved", "application.send_requested", "email.delivery"} <= actions


async def test_admin_application_uses_saved_vacancy_when_source_is_unavailable(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
) -> None:
    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="saved-unavailable-vacancy",
    )
    application_id = seeded["application_id"]
    async with sqlite_session_factory() as session:
        source_job = await session.get(SourceJob, seeded["source_job_id"])
        canonical = await session.get(CanonicalJob, seeded["canonical_job_id"])
        assert source_job is not None
        assert canonical is not None
        source_job.status = JobStatus.POSSIBLY_CLOSED
        source_job.confirmed_absence_count = 2
        source_job.description = "Saved description remains readable after the source disappears."
        source_job.requirements = "Python and careful production operations."
        source_job.responsibilities = "Maintain the application and review failures."
        canonical.status = JobStatus.POSSIBLY_CLOSED
        await session.commit()

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        csrf_token = await _login_admin(client, settings)
        detail = await client.get(f"/admin/applications/{application_id}")

        assert detail.status_code == 200
        assert "Вакансия временно не подтверждена" in detail.text
        assert "Saved description remains readable" in detail.text
        assert "Python and careful production operations" in detail.text
        assert "Maintain the application and review failures" in detail.text
        assert "Открыть исходную страницу" in detail.text
        assert "Одобрение недоступно" in detail.text
        assert f'action="/admin/applications/{application_id}/approve"' not in detail.text

        direct_approval = await client.post(
            f"/admin/applications/{application_id}/approve",
            data={"csrf_token": csrf_token},
        )
        assert direct_approval.status_code == 303
        assert direct_approval.headers["location"] == (
            f"/admin/applications/{application_id}?notice=application_approval_inactive_vacancy"
        )

        approval_notice = await client.get(direct_approval.headers["location"])
        assert approval_notice.status_code == 200
        assert "Вакансия больше не активна" in approval_notice.text

    async with sqlite_session_factory() as session:
        stored = await session.get(Application, application_id)
        assert stored is not None
        assert stored.status == ApplicationStatus.PENDING_REVIEW


async def test_admin_decision_queue_hides_markerless_stale_approval(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
) -> None:
    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="admin-stale-queue",
    )
    application_id = seeded["application_id"]
    profile_id = seeded["profile_id"]
    async with sqlite_session_factory() as session:
        stored = await session.get(Application, application_id)
        source_job = await session.get(SourceJob, seeded["source_job_id"])
        assert stored is not None
        assert source_job is not None
        stored.policy_result = {
            key: value
            for key, value in stored.policy_result.items()
            if key not in {"safe_stop_reason", "requires_rematch"}
        }
        source_job.content_hash = "f" * 64
        source_job.matching_content_hash = "f" * 64
        await session.commit()

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        await _login_admin(client, settings)
        queue = await client.get(f"/?view=decisions&profile_id={profile_id}")

        assert queue.status_code == 200
        assert "Вакансия изменилась — JobHunter выполняет повторный анализ." in queue.text
        assert f'action="/admin/applications/{application_id}/approve"' not in queue.text


async def test_admin_hides_approval_without_public_email_and_redirects_stale_post(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
) -> None:
    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="admin-internal-apply",
    )
    application_id = seeded["application_id"]
    profile_id = seeded["profile_id"]
    async with sqlite_session_factory() as session:
        stored = await session.get(Application, application_id)
        contact = await session.get(EmployerContact, seeded["contact_id"])
        assert stored is not None
        assert contact is not None
        contact.contact_type = ContactType.INTERNAL_JOB_BOARD
        contact.value = "https://jobs.example.test/jobs/admin-internal-apply"
        stored.content_validated = False
        stored.policy_result = {
            **stored.policy_result,
            "rules_failed": ["verified_email_contact", "letter_validated"],
        }
        await session.commit()

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        csrf_token = await _login_admin(client, settings)
        queue = await client.get(f"/?view=decisions&profile_id={profile_id}")
        assert queue.status_code == 200
        assert "нет публичного email" in queue.text
        assert f'action="/admin/applications/{application_id}/approve"' not in queue.text

        detail = await client.get(f"/admin/applications/{application_id}")
        assert detail.status_code == 200
        assert "Автоотправка недоступна" in detail.text
        assert "нет публичного email" in detail.text
        assert f'action="/admin/applications/{application_id}/approve"' not in detail.text

        direct_approval = await client.post(
            f"/admin/applications/{application_id}/approve",
            data={"csrf_token": csrf_token, "return_to": "decisions"},
        )
        assert direct_approval.status_code == 303
        assert direct_approval.headers["location"] == (
            f"/?view=decisions&profile_id={profile_id}&notice=application_approval_no_email"
        )

        approval_notice = await client.get(direct_approval.headers["location"])
        assert approval_notice.status_code == 200
        assert "Одобрение недоступно" in approval_notice.text
        assert "внутреннюю форму сайта" in approval_notice.text

    async with sqlite_session_factory() as session:
        stored = await session.get(Application, application_id)
        assert stored is not None
        assert stored.status == ApplicationStatus.PENDING_REVIEW
        feedback = await session.scalar(
            select(ReviewFeedbackEvent).where(ReviewFeedbackEvent.application_id == application_id)
        )
        assert feedback is None


async def test_admin_decision_queue_filters_and_rejects_without_sending(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
) -> None:
    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="admin-reject",
    )
    application_id = seeded["application_id"]
    profile_id = seeded["profile_id"]
    async with sqlite_session_factory() as session:
        base_application = await session.get(Application, application_id)
        assert base_application is not None
        for index in range(11):
            suffix = f"queue-{index}"
            canonical = CanonicalJob(
                normalized_company=f"queue company {index}",
                normalized_title=f"queue engineer {index}",
                normalized_location="chisinau",
                canonical_fingerprint=hashlib.sha256(suffix.encode()).hexdigest(),
                status=JobStatus.ACTIVE,
            )
            session.add(canonical)
            await session.flush()
            job = SourceJob(
                source_id=seeded["source_id"],
                canonical_job_id=canonical.id,
                external_job_id=suffix,
                canonical_url=f"https://jobs.example.test/jobs/{suffix}",
                localized_urls={"en": f"https://jobs.example.test/jobs/{suffix}"},
                title=f"Queue Engineer {index}",
                company=f"Queue Company {index}",
                categories_seen=["technology"],
                category="technology",
                description="Queue pagination fixture.",
                location="Chisinau",
                cities=["Chisinau"],
                public_email="jobs@example.test",
                page_locale="en",
                content_hash=hashlib.sha256(f"content-{suffix}".encode()).hexdigest(),
                source_fingerprint=hashlib.sha256(f"source-{suffix}".encode()).hexdigest(),
                status=JobStatus.ACTIVE,
                raw_metadata={},
            )
            job.matching_content_hash = compute_source_matching_hash(job)
            session.add(job)
            await session.flush()
            session.add(
                Application(
                    profile_id=profile_id,
                    canonical_job_id=canonical.id,
                    source_job_id=job.id,
                    match_evaluation_id=base_application.match_evaluation_id,
                    resume_id=base_application.resume_id,
                    recipient_contact_id=base_application.recipient_contact_id,
                    subject=f"Application for Queue Engineer {index}",
                    body="Queue pagination fixture body.",
                    language="en",
                    status=ApplicationStatus.PENDING_REVIEW,
                    policy_decision=PolicyDecision.PENDING_REVIEW,
                    policy_result={},
                    used_confirmed_facts=[],
                    content_validated=True,
                    idempotency_key=hashlib.sha256(f"application-{suffix}".encode()).hexdigest(),
                )
            )
        await session.commit()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        csrf_token = await _login_admin(client, settings)
        queue = await client.get(
            "/",
            params={"view": "decisions", "profile_id": str(profile_id)},
        )
        assert queue.status_code == 200
        assert "Queue Engineer" in queue.text
        assert "Отклонить" in queue.text
        assert "Обучение: собирает примеры" in queue.text
        assert "data-review-reject" in queue.text
        assert "Ещё 6 причинных решений по одному признаку до первых подсказок" in queue.text
        assert "Критерии и лимиты" not in queue.text
        assert len(HTMLParser(queue.text).css(".queue-card")) == 10
        assert "Страница 1 из 2" in queue.text

        second_page = await client.get(
            "/",
            params={
                "view": "decisions",
                "profile_id": str(profile_id),
                "page": "2",
            },
        )
        assert len(HTMLParser(second_page.text).css(".queue-card")) == 2
        assert "Backend Engineer" in second_page.text

        no_results = await client.get(
            "/",
            params={
                "view": "decisions",
                "profile_id": str(profile_id),
                "q": "definitely-not-present",
            },
        )
        assert "По этому фильтру откликов нет" in no_results.text

        rejected = await client.post(
            f"/admin/applications/{application_id}/reject",
            data={
                "csrf_token": csrf_token,
                "return_to": "decisions",
                "reason": "owner declined",
                "reason_code": "location",
                "learn_from_review": "true",
            },
        )
        assert rejected.status_code == 303
        assert rejected.headers["location"].startswith("/?view=decisions")

        paused_learning = await client.post(
            "/admin/review-learning/influence",
            data={
                "csrf_token": csrf_token,
                "profile_id": str(profile_id),
                "enabled": "false",
            },
        )
        assert paused_learning.status_code == 303
        paused_queue = await client.get(
            "/",
            params={"view": "decisions", "profile_id": str(profile_id)},
        )
        assert "Обучение: приостановлено" in paused_queue.text

        rejected_queue = await client.get(
            "/",
            params={
                "view": "decisions",
                "profile_id": str(profile_id),
                "status_filter": "cancelled",
            },
        )
        assert "Backend Engineer" in rejected_queue.text
        assert "Отменено" in rejected_queue.text

    async with sqlite_session_factory() as session:
        stored = await session.get(Application, application_id)
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "application.rejected_by_owner")
        )
        feedback = await session.scalar(
            select(ReviewFeedbackEvent).where(ReviewFeedbackEvent.application_id == application_id)
        )
        learning_setting = await session.scalar(
            select(ReviewLearningSetting).where(ReviewLearningSetting.profile_id == profile_id)
        )
        assert stored is not None
        assert stored.status == ApplicationStatus.CANCELLED
        assert audit is not None
        assert audit.decision == ApplicationStatus.CANCELLED.value
        assert audit.sanitized_details["reason"] == "owner declined"
        assert audit.sanitized_details["reason_code"] == "location"
        assert feedback is not None
        assert feedback.outcome == ReviewOutcome.REJECTED
        assert feedback.reason_code == ReviewReason.LOCATION
        assert feedback.learning_eligible is True
        assert feedback.feature_schema_version == FEATURE_SCHEMA_VERSION
        assert feedback.feature_snapshot["context"]["resume_category"] == "technology"
        assert feedback.feature_snapshot["learning_dimensions"] == [
            "city",
            "area",
            "workplace",
        ]
        assert learning_setting is not None
        assert learning_setting.influence_enabled is False


async def test_mcp_review_workflow_records_structured_learning_feedback(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.database.session as database_session
    from app.mcp import server as mcp_server

    _application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="mcp-review-learning",
    )
    application_id = seeded["application_id"]
    profile_id = seeded["profile_id"]
    monkeypatch.setattr(database_session, "async_session_factory", sqlite_session_factory)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)

    queue = await mcp_server.get_review_queue(profile_id=str(profile_id))
    result = await mcp_server.decide_review(
        str(application_id),
        "reject",
        reason_code="role",
        reason="Not my direction",
    )
    learning = await mcp_server.get_review_learning_status(profile_id=str(profile_id))

    assert queue["applications"][0]["application_id"] == str(application_id)
    assert queue["learning"]["status"] == "собирает примеры"
    assert result["status"] == "cancelled"
    assert result["review_outcome"] == "rejected"
    assert result["reason_code"] == "role"
    assert result["learning_eligible"] is True
    assert learning["rejected"] == 1

    async with sqlite_session_factory() as session:
        feedback = await session.scalar(
            select(ReviewFeedbackEvent).where(ReviewFeedbackEvent.application_id == application_id)
        )
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "application.rejected_by_owner")
        )
        assert feedback is not None
        assert feedback.actor == "mcp"
        assert feedback.reason_text == "Not my direction"
        assert audit is not None
        assert audit.actor == "mcp"


async def test_get_learning_model_status_reports_no_model_initially(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.database.session as database_session
    from app.mcp import server as mcp_server

    _application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="mcp-model-status",
    )
    profile_id = seeded["profile_id"]
    monkeypatch.setattr(database_session, "async_session_factory", sqlite_session_factory)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)

    status = await mcp_server.get_learning_model_status(profile_id=str(profile_id))

    assert status["profile_id"] == str(profile_id)
    assert status["segment_key"] == "global"
    assert status["model"] is None
    assert status["shadow"]["cases_total"] == 0


async def test_admin_review_queue_uses_learned_order_and_explanations(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
) -> None:
    from app.learning import ReviewLearningService

    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="learned-review-order",
    )
    profile_id = seeded["profile_id"]
    learning_service = ReviewLearningService()
    async with sqlite_session_factory() as session:
        profile = await session.get(UserProfile, profile_id)
        preferences = await session.scalar(
            select(JobPreference).where(JobPreference.profile_id == profile_id)
        )
        resume = await session.get(Resume, seeded["resume_id"])
        assert profile is not None
        assert preferences is not None
        assert resume is not None
        preferences.allowed_cities = ["Chisinau"]

        async def add_review(
            *,
            suffix: str,
            title: str,
            category: str,
            status: ApplicationStatus,
            outcome: ReviewOutcome | None,
        ) -> Application:
            canonical = CanonicalJob(
                normalized_company=f"learning company {suffix}",
                normalized_title=title.casefold(),
                normalized_location="chisinau",
                canonical_fingerprint=hashlib.sha256(
                    f"learning-canonical-{suffix}".encode()
                ).hexdigest(),
                status=JobStatus.ACTIVE,
            )
            session.add(canonical)
            await session.flush()
            job = SourceJob(
                source_id=seeded["source_id"],
                canonical_job_id=canonical.id,
                external_job_id=f"learning-{suffix}",
                canonical_url=f"https://jobs.example.test/learning/{suffix}",
                localized_urls={"en": f"https://jobs.example.test/learning/{suffix}"},
                title=title,
                company=f"Learning Company {suffix}",
                categories_seen=[category],
                category=category,
                description="Safe learned ordering fixture.",
                location="Chisinau",
                cities=["Chisinau"],
                schedule="full-time",
                public_email="jobs@example.test",
                page_locale="en",
                content_hash=hashlib.sha256(f"learning-content-{suffix}".encode()).hexdigest(),
                source_fingerprint=hashlib.sha256(f"learning-source-{suffix}".encode()).hexdigest(),
                status=JobStatus.ACTIVE,
                raw_metadata={},
            )
            job.matching_content_hash = compute_source_matching_hash(job)
            session.add(job)
            await session.flush()
            evaluation = MatchEvaluation(
                profile_id=profile_id,
                canonical_job_id=canonical.id,
                source_job_id=job.id,
                resume_fit=80,
                preference_fit=80,
                overall_fit=80,
                requirements_met=[],
                missing_requirements=[],
                risks=["manual_review_requested"],
                scam_indicators=[],
                explanation="Learned ordering fixture.",
                decision=MatchDecision.PREPARE_FOR_REVIEW,
                model="mock",
                prompt_rules_version="learning-test-v1",
                source_content_hash=job.content_hash,
                source_matching_hash=job.matching_content_hash,
                resume_id=resume.id,
                resume_sha256=resume.sha256,
                profile_fingerprint=profile_fingerprint(profile),
                preference_fingerprint=preference_fingerprint(preferences),
                confirmed_fact_hashes=confirmed_fact_hashes(profile),
            )
            contact = EmployerContact(
                canonical_job_id=canonical.id,
                source_job_id=job.id,
                value="jobs@example.test",
                contact_type=ContactType.EMAIL,
                discovery_source="vacancy",
                official_domain="example.test",
                verification_status=VerificationStatus.VERIFIED,
                confidence=1.0,
                evidence_url=job.canonical_url,
            )
            session.add_all([evaluation, contact])
            await session.flush()
            review = Application(
                profile_id=profile_id,
                canonical_job_id=canonical.id,
                source_job_id=job.id,
                match_evaluation_id=evaluation.id,
                resume_id=resume.id,
                recipient_contact_id=contact.id,
                subject=f"Application for {title}",
                body="Safe learned ordering fixture body.",
                language="en",
                status=status,
                policy_decision=PolicyDecision.PENDING_REVIEW,
                policy_result={"rules_failed": ["manual_review_requested"]},
                content_validated=True,
                idempotency_key=hashlib.sha256(
                    f"learning-application-{suffix}".encode()
                ).hexdigest(),
            )
            session.add(review)
            await session.flush()
            if outcome is not None:
                await learning_service.record_decision(
                    session,
                    review,
                    outcome=outcome,
                    actor="test",
                    reason=(ReviewReason.ROLE if outcome == ReviewOutcome.REJECTED else None),
                )
            return review

        for index in range(3):
            await add_review(
                suffix=f"accepted-{index}",
                title=f"Warehouse Picker {index}",
                category="warehouses",
                status=ApplicationStatus.APPROVED,
                outcome=ReviewOutcome.APPROVED,
            )
            await add_review(
                suffix=f"rejected-{index}",
                title=f"Sales Agent {index}",
                category="sales",
                status=ApplicationStatus.CANCELLED,
                outcome=ReviewOutcome.REJECTED,
            )
        await add_review(
            suffix="pending-warehouse",
            title="Learned Warehouse Picker",
            category="warehouses",
            status=ApplicationStatus.PENDING_REVIEW,
            outcome=None,
        )
        await add_review(
            suffix="pending-sales",
            title="Learned Sales Agent",
            category="sales",
            status=ApplicationStatus.PENDING_REVIEW,
            outcome=None,
        )
        await session.commit()

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        await _login_admin(client, settings)
        queue = await client.get(
            "/",
            params={"view": "decisions", "profile_id": str(profile_id)},
        )

    parsed = HTMLParser(queue.text)
    titles = [node.text(strip=True) for node in parsed.css(".queue-title a")]
    assert queue.status_code == 200
    assert titles.index("Learned Warehouse Picker") < titles.index("Learned Sales Agent")
    assert "Обучение: учитывает решения" in queue.text
    assert "Раньше вы чаще принимали" in queue.text
    assert "Раньше вы чаще отклоняли" in queue.text
    assert "чаще принимаете" in queue.text
    assert "чаще отклоняете" in queue.text
    assert "город:" not in queue.text


async def test_admin_vacancy_problem_is_excluded_from_preference_learning(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
) -> None:
    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="broken-vacancy-feedback",
    )
    application_id = seeded["application_id"]
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        csrf_token = await _login_admin(client, settings)
        response = await client.post(
            f"/admin/applications/{application_id}/reject",
            data={
                "csrf_token": csrf_token,
                "reason_code": "vacancy_problem",
                "learn_from_review": "true",
            },
        )
        assert response.status_code == 303

    async with sqlite_session_factory() as session:
        feedback = await session.scalar(
            select(ReviewFeedbackEvent).where(ReviewFeedbackEvent.application_id == application_id)
        )
        assert feedback is not None
        assert feedback.learning_eligible is False
        assert feedback.exclusion_reason == "vacancy_problem"


@pytest.mark.e2e
async def test_admin_review_learning_browser_flow_three_clean_contexts(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
) -> None:
    import asyncio
    import socket

    import uvicorn
    from fastapi.staticfiles import StaticFiles

    playwright_module = pytest.importorskip("playwright.async_api")
    async_playwright = playwright_module.async_playwright
    expect = playwright_module.expect

    application, settings = interface_app
    application.mount(
        "/admin-assets",
        StaticFiles(directory="app/admin/static"),
        name="browser-test-admin-assets",
    )
    seeded_reviews = [
        await _seed_review_application(
            sqlite_session_factory,
            settings,
            suffix=f"browser-learning-{index}",
        )
        for index in range(3)
    ]
    internal_review = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="browser-internal-apply",
    )
    stale_review = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="browser-stale-rematch",
    )
    vacancy_statuses = [JobStatus.POSSIBLY_CLOSED, JobStatus.ACTIVE, JobStatus.CLOSED]
    async with sqlite_session_factory() as session:
        for index, seeded in enumerate(seeded_reviews):
            source_job = await session.get(SourceJob, seeded["source_job_id"])
            canonical = await session.get(CanonicalJob, seeded["canonical_job_id"])
            assert source_job is not None
            assert canonical is not None
            source_job.status = vacancy_statuses[index]
            source_job.description = f"Saved browser vacancy description {index}."
            source_job.requirements = f"Browser requirement {index}."
            source_job.responsibilities = f"Browser responsibility {index}."
            canonical.status = vacancy_statuses[index]
        internal_application = await session.get(Application, internal_review["application_id"])
        stale_application = await session.get(Application, stale_review["application_id"])
        stale_source_job = await session.get(SourceJob, stale_review["source_job_id"])
        internal_contact = await session.get(EmployerContact, internal_review["contact_id"])
        internal_preferences = await session.scalar(
            select(JobPreference).where(JobPreference.profile_id == internal_review["profile_id"])
        )
        assert internal_application is not None
        assert stale_application is not None
        assert stale_source_job is not None
        assert internal_contact is not None
        assert internal_preferences is not None
        internal_resume = await session.get(Resume, internal_application.resume_id)
        assert internal_resume is not None
        internal_contact.contact_type = ContactType.INTERNAL_JOB_BOARD
        internal_contact.value = "https://jobs.example.test/jobs/browser-internal-apply"
        internal_application.content_validated = False
        internal_application.policy_result = {
            **internal_application.policy_result,
            "rules_failed": ["verified_email_contact", "letter_validated"],
        }
        stale_application.status = ApplicationStatus.PENDING_REVIEW
        stale_application.policy_decision = PolicyDecision.AUTO_APPROVED
        stale_application.content_validated = True
        stale_application.policy_result = {
            **stale_application.policy_result,
            "decision": "auto_approved",
            "rules_failed": [],
        }
        stale_source_job.content_hash = "f" * 64
        stale_source_job.matching_content_hash = "f" * 64
        internal_preferences.allowed_cities = ["Chisinau"]
        for index in range(3):
            session.add_all(
                [
                    ReviewFeedbackEvent(
                        profile_id=internal_review["profile_id"],
                        application_id=uuid4(),
                        match_evaluation_id=internal_application.match_evaluation_id,
                        canonical_job_id=internal_application.canonical_job_id,
                        source_job_id=internal_application.source_job_id,
                        outcome=ReviewOutcome.APPROVED,
                        actor="browser-city-fixture",
                        learning_eligible=True,
                        resume_sha256=internal_resume.sha256,
                        feature_schema_version="review-v1",
                        feature_snapshot={
                            "features": {
                                "category": ["warehouses"],
                                "title": ["picker"],
                                "city": ["Кишинев"],
                            },
                            "learning_dimensions": ["category", "title", "city"],
                        },
                    ),
                    ReviewFeedbackEvent(
                        profile_id=internal_review["profile_id"],
                        application_id=uuid4(),
                        match_evaluation_id=internal_application.match_evaluation_id,
                        canonical_job_id=internal_application.canonical_job_id,
                        source_job_id=internal_application.source_job_id,
                        outcome=ReviewOutcome.REJECTED,
                        reason_code=ReviewReason.ROLE,
                        actor="browser-city-fixture",
                        learning_eligible=True,
                        resume_sha256=internal_resume.sha256,
                        feature_schema_version="review-v1",
                        feature_snapshot={
                            "features": {
                                "category": ["sales"],
                                "title": [f"unrelated-{index}"],
                                "city": ["Бельцы"],
                            },
                            "learning_dimensions": ["category", "title"],
                        },
                    ),
                ]
            )
        await session.commit()
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = int(port_socket.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"
    settings.public_base_url = base_url
    server = uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    try:
        async with httpx.AsyncClient(base_url=base_url) as readiness_client:
            for _attempt in range(100):
                try:
                    response = await readiness_client.get("/login")
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("local browser test server did not start")

        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:  # pragma: no cover - depends on optional browser install
                pytest.skip(f"Playwright Chromium is unavailable: {type(exc).__name__}")
            try:
                viewports = [(390, 844), (768, 900), (1440, 900)]
                for index, seeded in enumerate(seeded_reviews):
                    width, height = viewports[index]
                    context = await browser.new_context(viewport={"width": width, "height": height})
                    page = await context.new_page()
                    await page.goto(f"{base_url}/login")
                    await page.get_by_label("Пароль администратора").fill(ADMIN_PASSWORD)
                    await page.get_by_role("button", name="Войти", exact=True).click()
                    await page.goto(
                        f"{base_url}/admin/applications/{stale_review['application_id']}"
                    )
                    await expect(
                        page.get_by_text(
                            "Вакансия изменилась — JobHunter выполняет повторный анализ."
                        )
                    ).to_be_visible()
                    await expect(
                        page.get_by_role("button", name="Одобрить", exact=True)
                    ).to_have_count(0)
                    await expect(
                        page.get_by_role("button", name="Одобрение недоступно")
                    ).to_be_disabled()
                    if index == 0:
                        stale_csrf = await page.locator(
                            'form[action$="/reject"] input[name="csrf_token"]'
                        ).input_value()
                        direct_approval = await context.request.post(
                            f"{base_url}/admin/applications/{stale_review['application_id']}/approve",
                            form={"csrf_token": stale_csrf},
                            max_redirects=0,
                        )
                        assert direct_approval.status == 303
                        assert direct_approval.headers["location"] == (
                            f"/admin/applications/{stale_review['application_id']}"
                            "?notice=application_approval_stale_evaluation"
                        )
                    await page.goto(f"{base_url}/admin/applications/{seeded['application_id']}")
                    await expect(
                        page.get_by_text(f"Saved browser vacancy description {index}.")
                    ).to_be_visible()
                    if vacancy_statuses[index] == JobStatus.ACTIVE:
                        await expect(
                            page.get_by_role("button", name="Одобрить", exact=True)
                        ).to_be_visible()
                    else:
                        await expect(
                            page.get_by_role("button", name="Одобрить", exact=True)
                        ).to_have_count(0)
                        await expect(
                            page.get_by_role("button", name="Одобрение недоступно")
                        ).to_be_disabled()
                    await page.get_by_role("button", name="Отклонить", exact=True).click()
                    detail_dialog = page.locator("dialog[data-confirm-dialog]")
                    await expect(detail_dialog).to_be_visible()
                    await expect(detail_dialog.get_by_text("Должность", exact=True)).to_be_visible()
                    await expect(detail_dialog.get_by_text("Зарплата", exact=True)).to_be_visible()
                    detail_dialog_box = await detail_dialog.bounding_box()
                    assert detail_dialog_box is not None
                    assert detail_dialog_box["x"] >= 0
                    assert detail_dialog_box["y"] >= 0
                    assert detail_dialog_box["x"] + detail_dialog_box["width"] <= width
                    assert detail_dialog_box["y"] + detail_dialog_box["height"] <= height
                    await detail_dialog.get_by_role("button", name="Отмена", exact=True).click()
                    await page.goto(f"{base_url}/?view=decisions&profile_id={seeded['profile_id']}")
                    await expect(
                        page.get_by_role("heading", name="Требуют решения")
                    ).to_be_visible()
                    await page.locator("details.learning-control > summary").click()
                    learning_popover = page.locator(".learning-popover")
                    await expect(learning_popover).to_be_visible()
                    await expect(learning_popover).to_contain_text("0 принято · 0 отклонено")
                    popover_box = await learning_popover.bounding_box()
                    assert popover_box is not None
                    assert popover_box["x"] >= 0
                    assert popover_box["x"] + popover_box["width"] <= width
                    await (
                        page.locator(".queue-card").get_by_role("button", name="Отклонить").click()
                    )
                    dialog = page.locator("dialog[data-confirm-dialog]")
                    await expect(dialog).to_be_visible()
                    await dialog.get_by_text("Место", exact=True).click()
                    if index == 2:
                        await dialog.locator("[data-review-learn]").uncheck()
                    await dialog.get_by_role("button", name="Отклонить", exact=True).click()
                    await expect(page.locator(".queue-card")).to_have_count(0)

                    # Reproduce a stale approve form in each clean context. The current UI
                    # must not offer approval without public email, and the server must
                    # redirect the stale POST back to a readable warning instead of JSON.
                    internal_detail_url = (
                        f"{base_url}/admin/applications/{internal_review['application_id']}"
                    )
                    await page.goto(internal_detail_url)
                    await expect(page.get_by_text("Автоотправка недоступна")).to_be_visible()
                    await expect(page.get_by_text("нет публичного email")).to_be_visible()
                    await expect(
                        page.get_by_role("button", name="Одобрить", exact=True)
                    ).to_have_count(0)
                    csrf_token = await page.locator(
                        'form[action$="/reject"] input[name="csrf_token"]'
                    ).input_value()
                    approval_action = (
                        f"/admin/applications/{internal_review['application_id']}/approve"
                    )
                    await page.evaluate(
                        """({ action, csrfToken }) => {
                            const form = document.createElement('form');
                            form.method = 'post';
                            form.action = action;
                            const csrf = document.createElement('input');
                            csrf.type = 'hidden';
                            csrf.name = 'csrf_token';
                            csrf.value = csrfToken;
                            form.appendChild(csrf);
                            document.body.appendChild(form);
                            form.submit();
                        }""",
                        {
                            "action": approval_action,
                            "csrfToken": csrf_token,
                        },
                    )
                    await page.wait_for_url(
                        f"{internal_detail_url}?notice=application_approval_no_email"
                    )
                    await expect(page.locator(".action-notice.warning")).to_contain_text(
                        "Одобрение недоступно"
                    )
                    await page.goto(
                        f"{base_url}/?view=decisions&profile_id={internal_review['profile_id']}"
                    )
                    await expect(
                        page.locator("details.learning-control > summary")
                    ).to_contain_text("Обучение: учитывает решения")
                    await expect(page.locator(".queue-learning-hint")).to_have_count(0)
                    await page.locator("details.learning-control > summary").click()
                    await expect(page.locator(".learning-popover")).not_to_contain_text("город:")
                    await context.close()
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await server_task

    async with sqlite_session_factory() as session:
        events = list((await session.scalars(select(ReviewFeedbackEvent))).all())
        browser_events = [event for event in events if event.actor == "admin"]
        assert len(browser_events) == 3
        assert all(event.reason_code == ReviewReason.LOCATION for event in browser_events)
        assert sum(event.learning_eligible for event in browser_events) == 2
        excluded = next(event for event in browser_events if not event.learning_eligible)
        assert excluded.exclusion_reason == "operator_opt_out"


async def test_rest_application_detail_and_audited_stale_delivery_reconciliation(
    interface_app: tuple[FastAPI, Settings], sqlite_session_factory: Any
) -> None:
    application, settings = interface_app
    seeded = await _seed_review_application(
        sqlite_session_factory,
        settings,
        suffix="rest-reconcile",
        status=ApplicationStatus.SENDING,
    )
    application_id = seeded["application_id"]
    stale_at = datetime.now(UTC) - timedelta(minutes=16)
    async with sqlite_session_factory() as session:
        session.add(
            EmailDelivery(
                application_id=application_id,
                provider="gmail",
                recipient="jobs@example.test",
                provider_message_id=None,
                thread_id=None,
                status=DeliveryStatus.SENDING,
                sanitized_provider_response={},
                attempt_count=1,
                created_at=stale_at,
                updated_at=stale_at,
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=application)
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        assert (await client.get(f"/api/v1/applications/{application_id}")).status_code == 401
        detail = await client.get(
            f"/api/v1/applications/{application_id}",
            headers=headers,
        )
        reconciled = await client.post(
            f"/api/v1/applications/{application_id}/reconcile-delivery-unknown",
            headers=headers,
        )
        second_reconciliation = await client.post(
            f"/api/v1/applications/{application_id}/reconcile-delivery-unknown",
            headers=headers,
        )

    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["body"].startswith("Hello Example Company")
    assert detail_payload["job"]["title"] == "Backend Engineer"
    assert detail_payload["resume"]["name"] == "Verified resume rest-reconcile"
    assert "storage_key" not in detail_payload["resume"]
    assert detail_payload["contact"]["evidence_url"].endswith("/rest-reconcile")
    assert detail_payload["failed_policy_rules"] == ["manual_review_requested"]
    assert detail_payload["delivery"]["status"] == "sending"
    assert detail_payload["delivery"]["can_reconcile_unknown"] is True
    assert reconciled.status_code == 200
    assert reconciled.json()["delivery_status"] == "delivery_unknown"
    assert second_reconciliation.status_code == 200

    async with sqlite_session_factory() as session:
        stored = await session.get(Application, application_id)
        delivery = await session.scalar(
            select(EmailDelivery).where(EmailDelivery.application_id == application_id)
        )
        assert stored is not None
        assert delivery is not None
        assert stored.status == ApplicationStatus.DELIVERY_UNKNOWN
        assert delivery.status == DeliveryStatus.DELIVERY_UNKNOWN
        reconciliations = list(
            (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.action == "email.delivery_reconciled")
                )
            ).all()
        )
        assert [event.decision for event in reconciliations] == [
            "delivery_unknown",
            "already_delivery_unknown",
        ]


async def test_admin_scan_queue_failure_marks_run_failed(
    interface_app: tuple[FastAPI, Settings],
    sqlite_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.database.session as database_session
    from app.scheduler import tasks as scheduler_tasks

    application, settings = interface_app
    source = JobSource(
        name="Queue failure source",
        base_url="https://fixture.invalid",
        adapter_type="fixture_source",
        configuration={},
        enabled=True,
        health_status=SourceHealth.HEALTHY,
    )
    async with sqlite_session_factory() as session:
        session.add(UserProfile(id=uuid4(), is_default=True, name="Scan Owner"))
        session.add(source)
        await session.commit()
        source_id = source.id

    monkeypatch.setattr(database_session, "async_session_factory", sqlite_session_factory)

    def unavailable_queue(*_: Any, **__: Any) -> None:
        raise RuntimeError("broker is unavailable")

    monkeypatch.setattr(scheduler_tasks.run_scan_task, "delay", unavailable_queue)
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        csrf_token = await _login_admin(client, settings)
        response = await client.post(
            f"/admin/sources/{source_id}/scan/full",
            data={"csrf_token": csrf_token},
        )

    assert response.status_code == 503
    async with sqlite_session_factory() as session:
        run = await session.scalar(select(ScanRun).where(ScanRun.source_id == source_id))
        assert run is not None
        assert run.status == RunStatus.FAILED
        assert run.diagnostics == {"queue_error": "RuntimeError"}


def _mcp_request(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _tool_payload(response: httpx.Response) -> dict[str, Any]:
    result = response.json()["result"]
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    return json.loads(result["content"][0]["text"])


async def test_mcp_streamable_http_auth_tools_secret_redaction_and_policy_gate(
    sqlite_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import app.database.session as database_session
    import app.main as main_module

    monkeypatch.setattr(database_session, "async_session_factory", sqlite_session_factory)
    monkeypatch.setattr(main_module.settings, "resume_storage_path", tmp_path / "resumes")
    main_module.settings.mcp_api_keys_hashed = [hash_api_key(API_KEY)]

    source = JobSource(
        name="Secret-bearing source",
        base_url=(
            "https://legacy-user:legacy-password@jobs.example.test/jobs"
            "?token=legacy-token&view=public"
        ),
        adapter_type="fixture_source",
        configuration={
            "selector": ".job",
            "api_key": "do-not-return-api-key",
            "legacy_url": (
                "https://legacy-user:legacy-password@jobs.example.test/feed?cursor=legacy-cursor"
            ),
            "nested": {
                "authorization": "Bearer do-not-return-bearer",
                "client_secret": "do-not-return-client-secret",
                "safe": "visible",
            },
        },
        health_status=SourceHealth.HEALTHY,
    )
    profile_id = uuid4()
    blocked_application = Application(
        profile_id=profile_id,
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        resume_id=uuid4(),
        recipient_contact_id=uuid4(),
        subject="Persisted subject",
        body="Persisted body",
        language="en",
        status=ApplicationStatus.PENDING_REVIEW,
        policy_decision=PolicyDecision.PENDING_REVIEW,
        policy_result={"rules_failed": ["manual_review_requested"]},
        idempotency_key=uuid4().hex,
        content_validated=True,
    )
    sending_application = Application(
        profile_id=profile_id,
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        resume_id=uuid4(),
        recipient_contact_id=uuid4(),
        subject="Sending subject",
        body="Sending body",
        language="en",
        status=ApplicationStatus.SENDING,
        idempotency_key=uuid4().hex,
        content_validated=True,
    )
    async with sqlite_session_factory() as session:
        session.add_all(
            [
                source,
                UserProfile(id=profile_id, is_default=True, name="MCP Owner"),
                JobPreference(
                    profile_id=profile_id,
                    allowed_schedules=["day"],
                    auto_send_enabled=False,
                    global_pause=True,
                ),
            ]
        )
        await session.flush()
        session.add_all([blocked_application, sending_application])
        await session.flush()
        stale_at = datetime.now(UTC) - timedelta(minutes=16)
        session.add(
            EmailDelivery(
                application_id=sending_application.id,
                provider="gmail",
                recipient="jobs@example.test",
                status=DeliveryStatus.SENDING,
                sanitized_provider_response={},
                attempt_count=1,
                created_at=stale_at,
                updated_at=stale_at,
            )
        )
        batch_ids: dict[str, str] = {}
        batch_states = {
            "running": [RunStatus.RUNNING, RunStatus.SUCCEEDED],
            "failed": [RunStatus.FAILED, RunStatus.CANCELLED],
            "partial": [RunStatus.SUCCEEDED, RunStatus.FAILED],
            "succeeded": [RunStatus.SUCCEEDED, RunStatus.SUCCEEDED],
        }
        for label, child_states in batch_states.items():
            children = [
                ScanRun(
                    source_id=source.id,
                    scan_type=ScanType.INCREMENTAL,
                    status=child_status,
                )
                for child_status in child_states
            ]
            session.add_all(children)
            await session.flush()
            batch = BatchScanRun(
                child_scan_ids=[str(child.id) for child in children],
                status=RunStatus.QUEUED,
                summary={},
            )
            session.add(batch)
            await session.flush()
            batch_ids[label] = str(batch.id)
        await session.commit()
        source_id = str(source.id)
        application_id = str(blocked_application.id)
        sending_application_id = str(sending_application.id)

    transport = httpx.ASGITransport(app=main_module.app, raise_app_exceptions=False)
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json, text/event-stream",
    }
    async with (
        main_module.app.router.lifespan_context(main_module.app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
        ) as client,
    ):
        unauthorized = await client.post("/mcp", json=_mcp_request(1, "tools/list", {}))
        assert unauthorized.status_code == 401

        hostile_origin = await client.post(
            "/mcp",
            headers={**headers, "Origin": "https://attacker.example"},
            json=_mcp_request(2, "tools/list", {}),
        )
        assert hostile_origin.status_code == 403

        initialized = await client.post(
            "/mcp",
            headers=headers,
            json=_mcp_request(
                3,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            ),
        )
        assert initialized.status_code == 200

        tools_response = await client.post(
            "/mcp",
            headers=headers,
            json=_mcp_request(4, "tools/list", {}),
        )
        tools = tools_response.json()["result"]["tools"]
        tool_names = {tool["name"] for tool in tools}
        assert tool_names == {
            "get_system_status",
            "get_user_profile",
            "list_user_profiles",
            "create_user_profile",
            "get_profile_by_id",
            "update_profile_by_id",
            "set_default_profile",
            "update_user_profile",
            "get_job_preferences",
            "update_job_preferences",
            "list_resumes",
            "upload_resume_metadata",
            "activate_resume",
            "deactivate_resume",
            "list_sources",
            "get_source",
            "add_source",
            "update_source",
            "enable_source",
            "disable_source",
            "validate_source",
            "discover_categories",
            "get_source_health",
            "start_full_scan",
            "start_incremental_scan",
            "start_all_sources_incremental_scan",
            "get_scan_status",
            "get_batch_scan_status",
            "list_recent_jobs",
            "list_job_matches",
            "get_job",
            "analyze_job",
            "prepare_application",
            "approve_application",
            "decide_review",
            "send_application",
            "list_applications",
            "get_review_queue",
            "get_review_learning_status",
            "get_learning_model_status",
            "set_review_learning_influence",
            "get_application_status",
            "reconcile_stale_application_delivery",
            "get_run_summary",
            "get_daily_report",
            "pause_auto_send",
            "resume_auto_send",
        }
        send_tool = next(tool for tool in tools if tool["name"] == "send_application")
        assert set(send_tool["inputSchema"]["properties"]) == {"application_id"}
        decide_tool = next(tool for tool in tools if tool["name"] == "decide_review")
        reason_schema = decide_tool["inputSchema"]
        assert reason_schema["properties"]["reason_code"]["$ref"] == "#/$defs/ReviewReason"
        assert set(reason_schema["$defs"]["ReviewReason"]["enum"]) == {
            reason.value for reason in ReviewReason
        }

        source_response = await client.post(
            "/mcp",
            headers=headers,
            json=_mcp_request(
                5,
                "tools/call",
                {"name": "get_source", "arguments": {"source_id": source_id}},
            ),
        )
        source_payload = source_response.json()
        serialized_source = source_response.text
        assert source_payload["result"].get("isError") is not True
        assert "do-not-return" not in serialized_source
        assert "legacy-user" not in serialized_source
        assert "legacy-password" not in serialized_source
        assert "legacy-token" not in serialized_source
        assert "legacy-cursor" not in serialized_source
        assert "jobs.example.test" in serialized_source
        assert "[redacted]" in serialized_source
        assert "visible" in serialized_source

        blocked_send = await client.post(
            "/mcp",
            headers=headers,
            json=_mcp_request(
                6,
                "tools/call",
                {
                    "name": "send_application",
                    "arguments": {
                        "application_id": application_id,
                        "recipient": "attacker@example.test",
                        "attachment": "/etc/passwd",
                    },
                },
            ),
        )
        blocked_payload = blocked_send.json()
        assert blocked_send.status_code == 200
        assert blocked_payload["result"]["isError"] is True

        application_detail = await client.post(
            "/mcp",
            headers=headers,
            json=_mcp_request(
                7,
                "tools/call",
                {
                    "name": "get_application_status",
                    "arguments": {"application_id": application_id},
                },
            ),
        )
        application_payload = _tool_payload(application_detail)
        assert application_payload["body"] == "Persisted body"
        assert application_payload["failed_policy_rules"] == ["manual_review_requested"]

        protected_preference_update = await client.post(
            "/mcp",
            headers=headers,
            json=_mcp_request(
                8,
                "tools/call",
                {
                    "name": "update_job_preferences",
                    "arguments": {
                        "preferences": {
                            "auto_send_enabled": True,
                            "global_pause": False,
                        }
                    },
                },
            ),
        )
        assert protected_preference_update.json()["result"]["isError"] is True

        ordinary_preference_update = await client.post(
            "/mcp",
            headers=headers,
            json=_mcp_request(
                9,
                "tools/call",
                {
                    "name": "update_job_preferences",
                    "arguments": {"preferences": {"allowed_categories": ["operations"]}},
                },
            ),
        )
        assert ordinary_preference_update.json()["result"].get("isError") is not True

        resumed = await client.post(
            "/mcp",
            headers=headers,
            json=_mcp_request(
                20,
                "tools/call",
                {"name": "resume_auto_send", "arguments": {}},
            ),
        )
        assert _tool_payload(resumed) == {
            "global_pause": False,
            "auto_send_enabled": True,
        }

        reconciled = await client.post(
            "/mcp",
            headers=headers,
            json=_mcp_request(
                21,
                "tools/call",
                {
                    "name": "reconcile_stale_application_delivery",
                    "arguments": {"application_id": sending_application_id},
                },
            ),
        )
        assert _tool_payload(reconciled)["delivery_status"] == "delivery_unknown"

        expected_batch_states = {
            "running": "running",
            "failed": "failed",
            "partial": "partial",
            "succeeded": "succeeded",
        }
        for offset, (label, expected_status) in enumerate(expected_batch_states.items(), start=10):
            batch_response = await client.post(
                "/mcp",
                headers=headers,
                json=_mcp_request(
                    offset,
                    "tools/call",
                    {
                        "name": "get_batch_scan_status",
                        "arguments": {"batch_id": batch_ids[label]},
                    },
                ),
            )
            batch_payload = _tool_payload(batch_response)
            assert batch_payload["status"] == expected_status
            assert batch_payload["summary"]["sources"] == 2
            assert batch_payload["summary"]["missing"] == 0
            assert batch_payload["started_at"] is not None
            if expected_status == "running":
                assert batch_payload["finished_at"] is None
            else:
                assert batch_payload["finished_at"] is not None

        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

    async with sqlite_session_factory() as session:
        stored = await session.get(Application, blocked_application.id)
        reconciled_application = await session.get(Application, sending_application.id)
        preferences = await session.scalar(select(JobPreference))
        assert stored is not None
        assert reconciled_application is not None
        assert preferences is not None
        assert stored.status == ApplicationStatus.PENDING_REVIEW
        assert reconciled_application.status == ApplicationStatus.DELIVERY_UNKNOWN
        assert preferences.allowed_categories == ["operations"]
        assert preferences.allowed_schedules == ["day"]
        assert preferences.auto_send_enabled is True
        assert preferences.global_pause is False
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "application.send_rejected")
        )
        assert audit is not None
        assert audit.actor == "mcp"
        reconcile_audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "email.delivery_reconciled")
        )
        assert reconcile_audit is not None
        assert reconcile_audit.actor == "mcp"
        persisted_batches = list((await session.scalars(select(BatchScanRun))).all())
        assert {batch.status.value for batch in persisted_batches} == {
            "running",
            "failed",
            "partial",
            "succeeded",
        }
