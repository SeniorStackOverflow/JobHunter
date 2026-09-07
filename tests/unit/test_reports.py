from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.matching.source_version import compute_source_matching_hash
from app.models.entities import (
    Application,
    CanonicalJob,
    EmailDelivery,
    EmployerContact,
    JobPreference,
    JobSource,
    MatchEvaluation,
    Resume,
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
    SourceHealth,
    VerificationStatus,
)
from app.reports.service import _generate


async def test_daily_report_counts_real_merges_and_distinguishes_auto_send(
    sqlite_session_factory,
) -> None:
    now = datetime.now(UTC)
    async with sqlite_session_factory() as session:
        source = JobSource(
            name="Board",
            base_url="https://jobs.example.com",
            adapter_type="generic_html",
            configuration={},
            health_status=SourceHealth.HEALTHY,
        )
        canonical = CanonicalJob(
            normalized_company="example",
            normalized_title="engineer",
            normalized_location="city",
            canonical_fingerprint="a" * 64,
            status=JobStatus.ACTIVE,
        )
        profile = UserProfile(name="Report Candidate")
        session.add(profile)
        await session.flush()
        session.add(
            JobPreference(
                profile_id=profile.id,
                maximum_daily_applications=20,
                additional_rules={
                    "minimum_daily_applications": 2,
                    "force_minimum_daily_applications": False,
                },
            )
        )
        resume = Resume(
            profile_id=profile.id,
            name="CV",
            category="technology",
            storage_key="resume.pdf",
            original_filename="resume.pdf",
            mime_type="application/pdf",
            sha256="b" * 64,
            active=True,
            verified=True,
        )
        session.add_all([source, canonical, resume])
        await session.flush()
        jobs = []
        for index in range(2):
            job = SourceJob(
                source_id=source.id,
                canonical_job_id=canonical.id,
                external_job_id=f"job-{index}",
                canonical_url=f"https://jobs.example.com/{index}",
                localized_urls={},
                title="Engineer",
                company="Example",
                categories_seen=["technology"],
                content_hash=str(index) * 64,
                source_fingerprint=str(index + 2) * 64,
                status=JobStatus.ACTIVE,
                raw_metadata={},
                first_seen_at=now,
            )
            job.matching_content_hash = compute_source_matching_hash(job)
            session.add(job)
            jobs.append(job)
        await session.flush()
        contact = EmployerContact(
            canonical_job_id=canonical.id,
            source_job_id=jobs[0].id,
            value="jobs@example.com",
            contact_type=ContactType.EMAIL,
            discovery_source="fixture",
            official_domain="example.com",
            verification_status=VerificationStatus.VERIFIED,
            confidence=1,
            evidence_url=jobs[0].canonical_url,
        )
        evaluation = MatchEvaluation(
            profile_id=profile.id,
            canonical_job_id=canonical.id,
            source_job_id=jobs[0].id,
            resume_fit=90,
            preference_fit=95,
            overall_fit=93,
            requirements_met=[],
            missing_requirements=[],
            risks=[],
            scam_indicators=[],
            explanation="fixture",
            decision=MatchDecision.AUTO_APPLY,
            model="mock",
            prompt_rules_version="v1",
            source_content_hash=jobs[0].content_hash,
            source_matching_hash=jobs[0].matching_content_hash,
            created_at=now + timedelta(seconds=2),
        )
        fallback_1 = MatchEvaluation(
            profile_id=profile.id,
            canonical_job_id=canonical.id,
            source_job_id=jobs[0].id,
            resume_fit=0,
            preference_fit=0,
            overall_fit=0,
            requirements_met=[],
            missing_requirements=[],
            risks=["llm_provider_failure:llmrouter:ReadTimeout"],
            scam_indicators=[],
            explanation="provider timeout",
            decision=MatchDecision.PREPARE_FOR_REVIEW,
            model="mock",
            prompt_rules_version="v1",
            source_content_hash=jobs[0].content_hash,
            source_matching_hash=jobs[0].matching_content_hash,
            created_at=now,
        )
        fallback_2 = MatchEvaluation(
            profile_id=profile.id,
            canonical_job_id=canonical.id,
            source_job_id=jobs[0].id,
            resume_fit=0,
            preference_fit=0,
            overall_fit=0,
            requirements_met=[],
            missing_requirements=[],
            risks=["llm_provider_failure:llmrouter:ReadTimeout"],
            scam_indicators=[],
            explanation="provider timeout retry",
            decision=MatchDecision.PREPARE_FOR_REVIEW,
            model="mock",
            prompt_rules_version="v1",
            source_content_hash=jobs[0].content_hash,
            source_matching_hash=jobs[0].matching_content_hash,
            created_at=now + timedelta(seconds=1),
        )
        session.add_all([contact, fallback_1, fallback_2, evaluation])
        await session.flush()
        application = Application(
            profile_id=profile.id,
            canonical_job_id=canonical.id,
            source_job_id=jobs[0].id,
            match_evaluation_id=evaluation.id,
            resume_id=resume.id,
            recipient_contact_id=contact.id,
            subject="Application",
            body="Body",
            language="en",
            status=ApplicationStatus.SENT,
            policy_decision=PolicyDecision.AUTO_APPROVED,
            policy_result={},
            content_validated=True,
            idempotency_key="c" * 64,
            sent_at=now,
        )
        session.add(application)
        await session.flush()
        session.add(
            EmailDelivery(
                application_id=application.id,
                provider="fake_gmail",
                recipient=contact.value,
                provider_message_id="message-1",
                thread_id="thread-1",
                status=DeliveryStatus.SENT,
                sanitized_provider_response={},
            )
        )
        await session.flush()

        report = await _generate(session)

        assert report.summary["duplicates_merged"] == 1
        assert report.summary["prepared"] == 1
        assert report.summary["auto_approved"] == 1
        assert report.summary["automatically_sent"] == 1
        assert report.summary["sent_total"] == 1
        assert report.summary["applications_created_today"] == 1
        assert report.summary["created_today_auto_approved"] == 1
        assert report.summary["created_today_pending_review"] == 0
        assert report.summary["created_today_blocked"] == 0
        assert report.summary["created_today_skipped"] == 0
        assert report.summary["created_today_unclassified"] == 0
        assert report.summary["sent_today"] == 1
        assert report.summary["sent_today_created_today"] == 1
        assert report.summary["sent_today_from_backlog"] == 0
        assert report.summary["sent_today_unclassified_origin"] == 0
        assert report.summary["unsent_auto_approved_backlog"] == 0
        assert report.summary["pending_review_backlog"] == 0
        assert report.summary["data_integrity"] == {"status": "ok", "issues": []}
        assert report.summary["timezone"] == "Europe/Chisinau"
        assert report.summary["period_start"].endswith(("+02:00", "+03:00"))
        assert report.summary["daily_limit"] == 20
        assert report.summary["daily_minimum"] == 2
        assert report.summary["daily_sent"] == 1
        assert report.summary["daily_limit_used"] == 1
        assert report.summary["daily_limit_remaining"] == 19
        assert report.summary["daily_minimum_remaining"] == 1
        assert report.summary["matching_attempts"] == 3
        assert report.summary["matching_evaluated"] == 1
        assert report.summary["matching_decisions"] == {
            "auto_apply": 1,
            "review": 0,
            "skip": 0,
            "block": 0,
        }
        assert report.summary["llm_provider_failures"] == {
            "total": 2,
            "unique_jobs": 1,
            "resolved_jobs": 1,
            "unresolved_jobs": 0,
            "by_code": {"llmrouter:ReadTimeout": 2},
        }
        assert report.summary["sent_applications"][0] == {
            "job_title": "Engineer",
            "company": "Example",
            "source": "Board",
            "overall_score": 93,
            "resume": "CV",
            "recipient": "jobs@example.com",
            "delivery_method": "fake_gmail",
            "sent_at": now.isoformat(),
            "application_id": str(application.id),
            "provider_message_id": "message-1",
            "thread_id": "thread-1",
            "automatic": True,
        }

        # Regression: a send event today may drain an application created on an
        # earlier day. It must not inflate today's created cohort.
        application.created_at = now - timedelta(days=2)
        await session.flush()
        backlog_report = await _generate(session)

        assert backlog_report.summary["applications_created_today"] == 0
        assert backlog_report.summary["created_today_auto_approved"] == 0
        assert backlog_report.summary["sent_today"] == 1
        assert backlog_report.summary["sent_today_created_today"] == 0
        assert backlog_report.summary["sent_today_from_backlog"] == 1
        assert backlog_report.summary["sent_today_unclassified_origin"] == 0
        assert backlog_report.summary["data_integrity"] == {"status": "ok", "issues": []}


async def test_daily_report_includes_learning_shadow_block(
    sqlite_session_factory,
) -> None:
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="p", is_default=True)
        session.add(profile)
        await session.flush()

        report = await _generate(session)

    assert "learning_shadow" in report.summary
    assert isinstance(report.summary["learning_shadow"], list)
    assert report.summary["learning_shadow"][0]["profile_id"] == str(profile.id)
