from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
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


async def _generate(session: AsyncSession) -> DailyReport:
    today = datetime.now(UTC).date()
    start = datetime.combine(today, time.min, UTC)
    end = start + timedelta(days=1)
    scans = list(
        (
            await session.scalars(
                select(ScanRun).where(ScanRun.started_at >= start, ScanRun.started_at < end)
            )
        ).all()
    )
    matched = int(
        await session.scalar(
            select(func.count(MatchEvaluation.id)).where(
                MatchEvaluation.created_at >= start,
                MatchEvaluation.created_at < end,
                MatchEvaluation.decision.in_(
                    [MatchDecision.AUTO_APPLY, MatchDecision.PREPARE_FOR_REVIEW]
                ),
            )
        )
        or 0
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
    prepared = int(
        await session.scalar(
            select(func.count(Application.id)).where(
                Application.created_at >= start,
                Application.created_at < end,
            )
        )
        or 0
    )
    auto_approved = int(
        await session.scalar(
            select(func.count(Application.id)).where(
                Application.created_at >= start,
                Application.created_at < end,
                Application.policy_decision == PolicyDecision.AUTO_APPROVED,
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
    summary = {
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
        "matching_jobs": matched,
        "prepared": prepared,
        "auto_approved": auto_approved,
        "automatically_sent": automatically_sent,
        "sent_total": len(sent_applications),
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
        "skipped": int(
            await session.scalar(
                select(func.count(MatchEvaluation.id)).where(
                    MatchEvaluation.decision == MatchDecision.SKIP,
                    MatchEvaluation.created_at >= start,
                    MatchEvaluation.created_at < end,
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
    }
    from app.learning.shadow import shadow_scorecard
    from app.profiles import ProfileService

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
