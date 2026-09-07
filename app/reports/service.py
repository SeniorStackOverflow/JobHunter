from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    Application,
    CanonicalJob,
    DailyReport,
    EmailDelivery,
    EmployerContact,
    JobSource,
    MatchEvaluation,
    Resume,
    ScanRun,
    SourceJob,
)
from app.models.enums import (
    ApplicationStatus,
    DeliveryStatus,
    MatchDecision,
    PolicyDecision,
    RunStatus,
)
from app.profiles import ProfileService
from app.time_utils import LOCAL_TIMEZONE_NAME, local_day_bounds


async def get_run_summary(session: AsyncSession, scan_id: UUID) -> dict[str, Any]:
    run = await session.get(ScanRun, scan_id)
    if run is None:
        raise LookupError(f"scan {scan_id} does not exist")
    return {
        "scan_id": str(run.id),
        "source_id": str(run.source_id),
        "status": run.status.value,
        "pages_checked": run.scanned_pages,
        "jobs_found": run.found_jobs,
        "new_jobs": run.new_jobs,
        "updated_jobs": run.updated_jobs,
        "unchanged_jobs": run.unchanged_jobs,
        "errors": run.parsing_errors + run.network_errors,
        "checkpoint": run.checkpoint,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _llm_failure_codes(risks: list[str] | None) -> list[str]:
    return [
        risk.removeprefix("llm_provider_failure:")
        for risk in (risks or [])
        if isinstance(risk, str) and risk.startswith("llm_provider_failure:")
    ]


async def _daily_matching_metrics(
    session: AsyncSession, start: datetime, end: datetime
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(
                MatchEvaluation.source_job_id,
                MatchEvaluation.decision,
                MatchEvaluation.risks,
                MatchEvaluation.created_at,
            )
            .where(
                MatchEvaluation.created_at >= start,
                MatchEvaluation.created_at < end,
            )
            .order_by(MatchEvaluation.created_at, MatchEvaluation.id)
        )
    ).all()
    latest_by_source: dict[UUID, tuple[MatchDecision, list[str] | None]] = {}
    failure_codes: dict[str, int] = {}
    failure_jobs: set[UUID] = set()
    for source_job_id, decision, risks, _created_at in rows:
        latest_by_source[source_job_id] = (decision, risks)
        codes = _llm_failure_codes(risks)
        if codes:
            failure_jobs.add(source_job_id)
        for code in codes:
            failure_codes[code] = failure_codes.get(code, 0) + 1

    final_counts = {
        MatchDecision.AUTO_APPLY: 0,
        MatchDecision.PREPARE_FOR_REVIEW: 0,
        MatchDecision.SKIP: 0,
        MatchDecision.BLOCK: 0,
    }
    unresolved_failures = 0
    for decision, risks in latest_by_source.values():
        if _llm_failure_codes(risks):
            unresolved_failures += 1
            continue
        final_counts[decision] = final_counts.get(decision, 0) + 1

    return {
        "matching_attempts": len(rows),
        "matching_evaluated": len(latest_by_source),
        "matching_jobs": (
            final_counts[MatchDecision.AUTO_APPLY] + final_counts[MatchDecision.PREPARE_FOR_REVIEW]
        ),
        "matching_decisions": {
            "auto_apply": final_counts[MatchDecision.AUTO_APPLY],
            "review": final_counts[MatchDecision.PREPARE_FOR_REVIEW],
            "skip": final_counts[MatchDecision.SKIP],
            "block": final_counts[MatchDecision.BLOCK],
        },
        "skipped": final_counts[MatchDecision.SKIP],
        "llm_provider_failures": {
            "total": sum(failure_codes.values()),
            "unique_jobs": len(failure_jobs),
            "resolved_jobs": max(0, len(failure_jobs) - unresolved_failures),
            "unresolved_jobs": unresolved_failures,
            "by_code": failure_codes,
        },
    }


async def _daily_limit_metrics(
    session: AsyncSession, start: datetime, end: datetime
) -> dict[str, Any]:
    profiles = ProfileService()
    profile = await profiles.get_profile(session)
    if profile is None:
        return {
            "daily_limit": None,
            "daily_minimum": None,
            "daily_minimum_forced": None,
            "daily_sent": None,
            "daily_limit_used": None,
            "daily_limit_remaining": None,
            "daily_minimum_remaining": None,
        }
    preference = await profiles.get_preferences(session, profile.id)
    rules = preference.additional_rules or {}
    try:
        minimum = max(0, int(rules.get("minimum_daily_applications", 0)))
    except (TypeError, ValueError):
        minimum = 0
    effective_minimum = min(minimum, preference.maximum_daily_applications)
    sent = int(
        await session.scalar(
            select(func.count(Application.id)).where(
                Application.profile_id == profile.id,
                Application.status == ApplicationStatus.SENT,
                Application.sent_at >= start,
                Application.sent_at < end,
            )
        )
        or 0
    )
    limit_used = int(
        await session.scalar(
            select(func.count(EmailDelivery.id))
            .join(Application, Application.id == EmailDelivery.application_id)
            .where(
                Application.profile_id == profile.id,
                EmailDelivery.created_at >= start,
                EmailDelivery.created_at < end,
                EmailDelivery.status.in_(
                    {
                        DeliveryStatus.SENT,
                        DeliveryStatus.SENDING,
                        DeliveryStatus.DELIVERY_UNKNOWN,
                    }
                ),
            )
        )
        or 0
    )
    return {
        "daily_limit": preference.maximum_daily_applications,
        "daily_minimum": minimum,
        "daily_effective_minimum": effective_minimum,
        "daily_minimum_forced": rules.get("force_minimum_daily_applications") is True,
        "daily_sent": sent,
        "daily_limit_used": limit_used,
        "daily_limit_remaining": max(0, preference.maximum_daily_applications - limit_used),
        "daily_minimum_remaining": max(0, effective_minimum - sent),
    }


async def _generate(session: AsyncSession) -> DailyReport:
    start_local, start, end = local_day_bounds()
    scans = list(
        (
            await session.scalars(
                select(ScanRun).where(ScanRun.started_at >= start, ScanRun.started_at < end)
            )
        ).all()
    )
    new_source_jobs = int(
        await session.scalar(
            select(func.count(SourceJob.id)).where(
                SourceJob.first_seen_at >= start,
                SourceJob.first_seen_at < end,
            )
        )
        or 0
    )
    new_canonical_jobs = int(
        await session.scalar(
            select(func.count(CanonicalJob.id)).where(
                CanonicalJob.created_at >= start,
                CanonicalJob.created_at < end,
            )
        )
        or 0
    )
    sent_rows = list(
        (
            await session.execute(
                select(
                    EmailDelivery,
                    Application,
                    SourceJob,
                    JobSource,
                    Resume,
                    EmployerContact,
                )
                .join(Application, Application.id == EmailDelivery.application_id)
                .join(SourceJob, SourceJob.id == Application.source_job_id)
                .join(JobSource, JobSource.id == SourceJob.source_id)
                .join(Resume, Resume.id == Application.resume_id)
                .join(EmployerContact, EmployerContact.id == Application.recipient_contact_id)
                .where(
                    EmailDelivery.status == DeliveryStatus.SENT,
                    Application.sent_at >= start,
                    Application.sent_at < end,
                )
                .order_by(Application.sent_at)
            )
        ).all()
    )
    sent_applications: list[dict[str, Any]] = []
    automatically_sent = 0
    for delivery, application, job, source, resume, contact in sent_rows:
        evaluation = await session.scalar(
            select(MatchEvaluation)
            .where(
                MatchEvaluation.profile_id == application.profile_id,
                MatchEvaluation.canonical_job_id == application.canonical_job_id,
            )
            .order_by(MatchEvaluation.created_at.desc())
            .limit(1)
        )
        automatic = application.policy_decision == PolicyDecision.AUTO_APPROVED
        automatically_sent += int(automatic)
        sent_applications.append(
            {
                "job_title": job.title,
                "company": job.company,
                "source": source.name,
                "overall_score": evaluation.overall_fit if evaluation else None,
                "resume": resume.name,
                "recipient": delivery.recipient or contact.value,
                "delivery_method": delivery.provider,
                "sent_at": application.sent_at.isoformat() if application.sent_at else None,
                "application_id": str(application.id),
                "provider_message_id": delivery.provider_message_id,
                "thread_id": delivery.thread_id,
                "automatic": automatic,
            }
        )
    created_policy_rows = (
        await session.execute(
            select(Application.policy_decision, func.count(Application.id))
            .where(
                Application.created_at >= start,
                Application.created_at < end,
            )
            .group_by(Application.policy_decision)
        )
    ).all()
    created_policy_counts = {decision: int(count) for decision, count in created_policy_rows}
    prepared = sum(created_policy_counts.values())
    auto_approved = created_policy_counts.get(PolicyDecision.AUTO_APPROVED, 0)
    created_today_pending_review = created_policy_counts.get(PolicyDecision.PENDING_REVIEW, 0)
    created_today_blocked = created_policy_counts.get(PolicyDecision.BLOCKED, 0)
    created_today_skipped = created_policy_counts.get(PolicyDecision.SKIPPED, 0)
    created_today_unclassified = created_policy_counts.get(None, 0)

    sent_today_created_today = int(
        await session.scalar(
            select(func.count(EmailDelivery.id))
            .join(Application, Application.id == EmailDelivery.application_id)
            .where(
                EmailDelivery.status == DeliveryStatus.SENT,
                Application.sent_at >= start,
                Application.sent_at < end,
                Application.created_at >= start,
                Application.created_at < end,
            )
        )
        or 0
    )
    sent_today_from_backlog = int(
        await session.scalar(
            select(func.count(EmailDelivery.id))
            .join(Application, Application.id == EmailDelivery.application_id)
            .where(
                EmailDelivery.status == DeliveryStatus.SENT,
                Application.sent_at >= start,
                Application.sent_at < end,
                Application.created_at < start,
            )
        )
        or 0
    )
    sent_today_unclassified_origin = max(
        0, len(sent_applications) - sent_today_created_today - sent_today_from_backlog
    )

    unsent_auto_approved_backlog = int(
        await session.scalar(
            select(func.count(Application.id)).where(
                Application.status == ApplicationStatus.AUTO_APPROVED
            )
        )
        or 0
    )
    pending_review_backlog = int(
        await session.scalar(
            select(func.count(Application.id)).where(
                Application.status == ApplicationStatus.PENDING_REVIEW
            )
        )
        or 0
    )
    delivery_errors = int(
        await session.scalar(
            select(func.count(EmailDelivery.id)).where(
                EmailDelivery.updated_at >= start,
                EmailDelivery.updated_at < end,
                EmailDelivery.status.in_(
                    [
                        DeliveryStatus.DELIVERY_UNKNOWN,
                        DeliveryStatus.TEMPORARY_FAILURE,
                        DeliveryStatus.PERMANENT_FAILURE,
                    ]
                ),
            )
        )
        or 0
    )
    scan_errors = sum(scan.parsing_errors + scan.network_errors for scan in scans)
    matching_metrics = await _daily_matching_metrics(session, start, end)
    limit_metrics = await _daily_limit_metrics(session, start, end)
    summary = {
        "calendar_date": start_local.date().isoformat(),
        "timezone": LOCAL_TIMEZONE_NAME,
        "period_start": start_local.isoformat(),
        "period_start_utc": start.isoformat(),
        "period_end_utc": end.isoformat(),
        "sources_checked": len({str(scan.source_id) for scan in scans}),
        "pages_checked": sum(scan.scanned_pages for scan in scans),
        "jobs_found": sum(scan.found_jobs for scan in scans),
        "new_jobs": sum(scan.new_jobs for scan in scans),
        "updated_jobs": sum(scan.updated_jobs for scan in scans),
        "rechecked_old_jobs": int(
            await session.scalar(
                select(func.count(SourceJob.id)).where(
                    SourceJob.last_checked_at >= start,
                    SourceJob.last_checked_at < end,
                )
            )
            or 0
        ),
        "duplicates_merged": max(0, new_source_jobs - new_canonical_jobs),
        **matching_metrics,
        # Legacy counters are retained for compatibility. The explicit fields below
        # distinguish today's application cohort from send events that may drain
        # applications created on earlier days.
        "prepared": prepared,
        "auto_approved": auto_approved,
        "applications_created_today": prepared,
        "created_today_auto_approved": auto_approved,
        "created_today_pending_review": created_today_pending_review,
        "created_today_blocked": created_today_blocked,
        "created_today_skipped": created_today_skipped,
        "created_today_unclassified": created_today_unclassified,
        "automatically_sent": automatically_sent,
        "sent_total": len(sent_applications),
        "sent_today": len(sent_applications),
        "sent_today_created_today": sent_today_created_today,
        "sent_today_from_backlog": sent_today_from_backlog,
        "sent_today_unclassified_origin": sent_today_unclassified_origin,
        "unsent_auto_approved_backlog": unsent_auto_approved_backlog,
        "pending_review_backlog": pending_review_backlog,
        "sent_applications": sent_applications,
        "pending_review": int(
            await session.scalar(
                select(func.count(Application.id)).where(
                    Application.status == ApplicationStatus.PENDING_REVIEW,
                    Application.created_at >= start,
                    Application.created_at < end,
                )
            )
            or 0
        ),
        "blocked": int(
            await session.scalar(
                select(func.count(Application.id)).where(
                    Application.status == ApplicationStatus.BLOCKED,
                    Application.created_at >= start,
                    Application.created_at < end,
                )
            )
            or 0
        ),
        "errors": scan_errors + delivery_errors,
        "email_delivery_errors": delivery_errors,
        "failed_scans": sum(scan.status == RunStatus.FAILED for scan in scans),
        "data_integrity": {
            "status": (
                "ok"
                if created_today_unclassified == 0 and sent_today_unclassified_origin == 0
                else "INCONSISTENT_REPORT_DATA"
            ),
            "issues": [
                *(
                    [f"created_today_unclassified:{created_today_unclassified}"]
                    if created_today_unclassified
                    else []
                ),
                *(
                    [f"sent_today_unclassified_origin:{sent_today_unclassified_origin}"]
                    if sent_today_unclassified_origin
                    else []
                ),
            ],
        },
        **limit_metrics,
    }
    from app.learning.shadow import shadow_scorecard

    summary["learning_shadow"] = [
        await shadow_scorecard(session, profile.id)
        for profile in await ProfileService().list_profiles(session)
    ]
    existing = await session.scalar(select(DailyReport).where(DailyReport.report_date == start))
    if existing is None:
        existing = DailyReport(report_date=start, summary=summary)
        session.add(existing)
    else:
        existing.summary = summary
    await session.flush()
    return existing


async def generate_daily_report() -> dict[str, Any]:
    from app.database.session import async_session_factory

    async with async_session_factory() as session:
        report = await _generate(session)
        await session.commit()
        return dict(report.summary)
