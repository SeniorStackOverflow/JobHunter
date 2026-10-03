from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.daily_target import (
    TARGET_POLICY_VERSION,
    daily_target_state,
    minimum_catchup_active,
    minimum_catchup_score,
    minimum_catchup_scores,
    minimum_daily_requirement,
)
from app.delivery_ledger import COUNTED_OUTCOMES, local_day_key, transmissions_on_day
from app.matching.freshness import count_profile_matching_backlog
from app.models.entities import (
    Alert,
    Application,
    CanonicalJob,
    DailyReport,
    EmailDelivery,
    EmailSendAttempt,
    EmployerContact,
    EmployerIdentityCandidate,
    EmployerRelationship,
    JobPreference,
    JobSource,
    MatchEvaluation,
    Resume,
    ScanRun,
    SourceJob,
)
from app.models.enums import (
    ApplicationStatus,
    DeliveryStatus,
    EmployerRelationshipState,
    JobStatus,
    MatchDecision,
    PolicyDecision,
    RunStatus,
)
from app.profiles import ProfileService
from app.reports.phone_metrics import daily_phone_metrics
from app.settings import get_settings
from app.telemetry import external_call_metrics
from app.time_utils import LOCAL_TIMEZONE_NAME, local_day_bounds

REPORT_COUNTER_DEFINITIONS: dict[str, str] = {
    "external_calls.total_attempts": "provider attempts (each Router candidate try)",
    "external_calls.logical_requests": "application-level LLM requests",
    "external_calls.recovered_requests": (
        "legacy name of transport_recovered_requests: a provider attempt failed and a "
        "later one returned HTTP success; says nothing about output validity"
    ),
    "external_calls.transport_recovered_requests": "same as recovered_requests",
    "external_calls.final_failed_requests": "requests whose last transport attempt failed",
    "external_calls.schema_invalid_requests": (
        "requests whose model output failed local validation after bounded repair and fell "
        "back to manual review (score 0)"
    ),
    "external_calls.router_synthetic_failures": (
        "attempts reported as 5xx by Router but originating from another upstream status "
        "(structured-output 400, truncated 200); see router_synthetic_by_status"
    ),
    "email_delivery.provider_submissions": (
        "hard-maximum ledger for this local day: accepted, in-flight and unknown provider "
        "submissions; a later bounce does not release it"
    ),
    "email_delivery.initially_accepted": "messages first accepted by the provider today",
    "email_delivery.submitted_cohort_current_status": (
        "current delivery status of messages first submitted today; provider acceptance "
        "does not prove inbox delivery"
    ),
    "sent_applications": (
        "today's sends whose current delivery status is still accepted/delivered/unknown; "
        "bounced sends are excluded here but remain in provider_submissions"
    ),
    "sent_applications[].overall_score": (
        "score of the evaluation the send was authorized with (bound evaluation); "
        "latest_evaluation_score is a later re-evaluation, if any"
    ),
    "daily_limit_used": "provider_submissions of the default profile (hard maximum)",
    "daily_sent": "confirmed successful sends of the default profile (daily minimum)",
}


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
    session: AsyncSession,
    start: datetime,
    end: datetime,
    *,
    profile_id: UUID | None = None,
) -> dict[str, Any]:
    query = (
        select(
            MatchEvaluation.source_job_id,
            MatchEvaluation.decision,
            MatchEvaluation.risks,
            MatchEvaluation.created_at,
            MatchEvaluation.llm_outcome,
        )
        .where(
            MatchEvaluation.created_at >= start,
            MatchEvaluation.created_at < end,
        )
        .order_by(MatchEvaluation.created_at, MatchEvaluation.id)
    )
    if profile_id is not None:
        query = query.where(MatchEvaluation.profile_id == profile_id)
    rows = (await session.execute(query)).all()
    latest_by_source: dict[UUID, tuple[MatchDecision, list[str] | None]] = {}
    failure_codes: dict[str, int] = {}
    failure_jobs: set[UUID] = set()
    llm_outcomes: dict[str, int] = {}
    for source_job_id, decision, risks, _created_at, llm_outcome in rows:
        latest_by_source[source_job_id] = (decision, risks)
        outcome_key = llm_outcome or "unrecorded"
        llm_outcomes[outcome_key] = llm_outcomes.get(outcome_key, 0) + 1
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
        # Per evaluation: valid (schema validated), invalid_output (fallback to
        # review), not_called (deterministic result), unrecorded (pre-migration).
        "llm_evaluation_outcomes": llm_outcomes,
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
    effective_minimum = minimum_daily_requirement(preference)
    target = await daily_target_state(session, preference, now=start)
    sent = target.sent
    progress = sent + target.reserved
    limit_used = await transmissions_on_day(
        session, profile_id=profile.id, day=local_day_key(start)
    )
    return {
        "daily_limit": preference.maximum_daily_applications,
        "daily_minimum": minimum,
        "daily_effective_minimum": effective_minimum,
        "daily_minimum_forced": effective_minimum > 0,
        "daily_minimum_required": effective_minimum > 0,
        "daily_minimum_catchup_active": minimum_catchup_active(preference, progress),
        "daily_minimum_catchup_score": minimum_catchup_score(preference),
        "daily_minimum_catchup_stages": list(minimum_catchup_scores(preference)),
        "daily_minimum_effective_score": (
            minimum_catchup_scores(preference)[-1]
            if minimum_catchup_active(preference, progress)
            else preference.minimum_auto_send_score
        ),
        "daily_minimum_normal_score": preference.minimum_auto_send_score,
        "daily_sent": sent,
        "daily_limit_used": limit_used,
        "daily_limit_remaining": max(0, preference.maximum_daily_applications - limit_used),
        "daily_minimum_remaining": target.remaining,
        "daily_minimum_confirmed_deficit": max(0, effective_minimum - sent),
    }


async def _daily_minimum_diagnostics(
    session: AsyncSession,
    start: datetime,
    end: datetime,
    *,
    profile_id: UUID | None,
    minimum: int,
    sent: int,
) -> dict[str, Any]:
    preference = await session.scalar(
        select(JobPreference).where(JobPreference.profile_id == profile_id)
    )
    safe_ready = (
        (await daily_target_state(session, preference, now=start)).reserved
        if preference is not None
        else 0
    )
    rows = (
        await session.execute(
            select(Application.status, Application.policy_result)
            .join(
                MatchEvaluation,
                MatchEvaluation.id == Application.match_evaluation_id,
            )
            .where(
                Application.profile_id == profile_id,
                MatchEvaluation.profile_id == profile_id,
                MatchEvaluation.created_at >= start,
                MatchEvaluation.created_at < end,
                Application.status != ApplicationStatus.SENT,
            )
        )
    ).all()
    blockers = {
        "employer_safety": 0,
        "auto_send_category": 0,
        "hard_requirements": 0,
        "delivery_safety": 0,
        "other_policy": 0,
    }
    primary_blockers = dict.fromkeys(blockers, 0)
    employer_only = 0
    for _status, policy_result in rows:
        failed = {
            item for item in (policy_result or {}).get("rules_failed", []) if isinstance(item, str)
        }
        employer_rules = {
            "no_active_employer_conversation",
            "employer_application_slot_available",
            "distinct_employer_today",
        }
        if failed and failed <= employer_rules:
            employer_only += 1
        primary = next(
            (
                name
                for name, rules in (
                    (
                        "hard_requirements",
                        {
                            "mandatory_requirements_met",
                            "deterministic_hard_requirements_met",
                            "hard_requirement_binding_current",
                            "no_material_match_risk",
                        },
                    ),
                    (
                        "delivery_safety",
                        {
                            "verified_email_contact",
                            "contact_verified",
                            "contact_delivery_usable",
                            "letter_validated",
                            "resume_active_verified",
                            "no_delivery_unknown",
                            "not_previously_sent",
                        },
                    ),
                    (
                        "auto_send_category",
                        {"category_allowed_for_auto_send", "job_title_allowed_by_preferences"},
                    ),
                    ("employer_safety", employer_rules),
                )
                if rules & failed
            ),
            "other_policy",
        )
        primary_blockers[primary] += 1
        if employer_rules & failed:
            blockers["employer_safety"] += 1
        if "category_allowed_for_auto_send" in failed:
            blockers["auto_send_category"] += 1
        if {
            "mandatory_requirements_met",
            "deterministic_hard_requirements_met",
            "hard_requirement_binding_current",
        } & failed:
            blockers["hard_requirements"] += 1
        if {
            "contact_delivery_usable",
            "not_previously_sent",
            "no_delivery_unknown",
            "verified_email_contact",
            "contact_verified",
        } & failed:
            blockers["delivery_safety"] += 1
        if failed and not (
            {
                "no_active_employer_conversation",
                "employer_application_slot_available",
                "category_allowed_for_auto_send",
                "mandatory_requirements_met",
                "deterministic_hard_requirements_met",
                "hard_requirement_binding_current",
                "contact_delivery_usable",
                "not_previously_sent",
                "no_delivery_unknown",
                "verified_email_contact",
                "contact_verified",
            }
            & failed
        ):
            blockers["other_policy"] += 1

    if minimum <= 0:
        status = "disabled"
    elif sent >= minimum:
        status = "met"
    elif safe_ready >= minimum - sent:
        status = "catchup_ready"
    elif safe_ready > 0:
        status = "catchup_partial"
    elif sum(value > 0 for value in primary_blockers.values()) > 1:
        status = "mixed_constraints"
    elif primary_blockers["employer_safety"] > 0:
        status = "employer_safety_constrained"
    else:
        status = "no_safe_candidates"
    return {
        "daily_minimum_status": status,
        "safe_auto_send_ready": safe_ready,
        "daily_minimum_blockers": blockers,
        "daily_minimum_primary_blockers": primary_blockers,
        "daily_minimum_employer_only": employer_only,
        "daily_minimum_employer_with_other_blockers": max(
            0, blockers["employer_safety"] - employer_only
        ),
        "daily_minimum_cohort": "day_evaluations_with_current_application_status",
    }


_REPORT_SECTION_TIMEOUT_SECONDS = 8.0


async def _profile_matching_backlog(
    session: AsyncSession,
    profile_id: UUID | None,
) -> int:
    if profile_id is None:
        return 0
    profiles = ProfileService()
    profile = await profiles.get_processing_profile(session, profile_id)
    if profile is None:
        return 0
    preference = await profiles.get_preferences(session, profile.id)
    return await count_profile_matching_backlog(
        session,
        profile,
        preference,
        get_settings(),
    )


async def _bounded_matching_backlog(profile_id: UUID | None) -> tuple[int | None, str]:
    from app.database.session import async_session_factory

    async def _run() -> int:
        async with async_session_factory() as diagnostics_session:
            return await _profile_matching_backlog(diagnostics_session, profile_id)

    try:
        return await asyncio.wait_for(_run(), timeout=_REPORT_SECTION_TIMEOUT_SECONDS), "ok"
    except TimeoutError:
        return None, "timeout"


async def _bounded_minimum_diagnostics(
    start: datetime,
    end: datetime,
    *,
    profile_id: UUID | None,
    minimum: int,
    sent: int,
) -> dict[str, Any]:
    from app.database.session import async_session_factory

    async def _run() -> dict[str, Any]:
        async with async_session_factory() as diagnostics_session:
            return await _daily_minimum_diagnostics(
                diagnostics_session,
                start,
                end,
                profile_id=profile_id,
                minimum=minimum,
                sent=sent,
            )

    try:
        result = await asyncio.wait_for(_run(), timeout=_REPORT_SECTION_TIMEOUT_SECONDS)
        return {**result, "daily_minimum_diagnostics_state": "ok"}
    except TimeoutError:
        return {
            "daily_minimum_status": "unavailable_timeout",
            "safe_auto_send_ready": None,
            "daily_minimum_blockers": {},
            "daily_minimum_diagnostics_state": "timeout",
        }


async def _generate(
    session: AsyncSession, *, persist: bool = True, day: date | None = None
) -> DailyReport:
    collection_started_at = datetime.now(UTC)
    start_local, start, end = local_day_bounds(day=day)
    current_local, _, _ = local_day_bounds()
    finalized = start_local.date() < current_local.date()
    if finalized:
        previous = await session.scalar(select(DailyReport).where(DailyReport.report_date == start))
        if previous is not None and previous.summary.get("finalized") is True:
            return previous
    report_profile = await ProfileService().get_profile(session)
    report_profile_id = report_profile.id if report_profile is not None else None
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
                    EmailDelivery.status.in_(
                        {
                            DeliveryStatus.SENT,
                            DeliveryStatus.PROVIDER_ACCEPTED,
                            DeliveryStatus.DELIVERED,
                            DeliveryStatus.DELIVERY_UNKNOWN,
                        }
                    ),
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
        # The historical score is the evaluation the send was authorized with.
        # A later re-evaluation (even an invalid score 0) never rewrites it.
        bound_evaluation = (
            await session.get(MatchEvaluation, application.match_evaluation_id)
            if application.match_evaluation_id is not None
            else None
        )
        if bound_evaluation is not None and (
            bound_evaluation.profile_id != application.profile_id
            or bound_evaluation.canonical_job_id != application.canonical_job_id
        ):
            bound_evaluation = None
        latest_evaluation = await session.scalar(
            select(MatchEvaluation)
            .where(
                MatchEvaluation.profile_id == application.profile_id,
                MatchEvaluation.canonical_job_id == application.canonical_job_id,
            )
            .order_by(MatchEvaluation.created_at.desc())
            .limit(1)
        )
        authorization = (application.policy_result or {}).get("send_authorization")
        authorization = authorization if isinstance(authorization, dict) else {}
        automatic = application.policy_decision == PolicyDecision.AUTO_APPROVED
        automatically_sent += int(automatic)
        sent_applications.append(
            {
                "job_title": job.title,
                "company": job.company,
                "source": source.name,
                "overall_score": (
                    bound_evaluation.overall_fit if bound_evaluation is not None else None
                ),
                "overall_score_source": (
                    "bound_evaluation" if bound_evaluation is not None else "unknown"
                ),
                "match_evaluation_id": (
                    str(bound_evaluation.id) if bound_evaluation is not None else None
                ),
                "send_threshold": authorization.get("effective_threshold"),
                "latest_evaluation_score": (
                    latest_evaluation.overall_fit if latest_evaluation is not None else None
                ),
                "latest_evaluation_id": (
                    str(latest_evaluation.id) if latest_evaluation is not None else None
                ),
                "resume": resume.name,
                "recipient": delivery.recipient or contact.value,
                "delivery_method": delivery.provider,
                "sent_at": application.sent_at.isoformat() if application.sent_at else None,
                "application_id": str(application.id),
                "provider_message_id": delivery.provider_message_id,
                "thread_id": delivery.thread_id,
                "delivery_status": delivery.status.value,
                "smtp_status": delivery.smtp_status,
                "failure_class": delivery.failure_class,
                "bounced_at": delivery.bounced_at.isoformat() if delivery.bounced_at else None,
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
                EmailDelivery.status.in_(
                    {
                        DeliveryStatus.SENT,
                        DeliveryStatus.PROVIDER_ACCEPTED,
                        DeliveryStatus.DELIVERED,
                        DeliveryStatus.DELIVERY_UNKNOWN,
                    }
                ),
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
                EmailDelivery.status.in_(
                    {
                        DeliveryStatus.SENT,
                        DeliveryStatus.PROVIDER_ACCEPTED,
                        DeliveryStatus.DELIVERED,
                        DeliveryStatus.DELIVERY_UNKNOWN,
                    }
                ),
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
                        DeliveryStatus.BOUNCED_TRANSIENT,
                        DeliveryStatus.BOUNCED_PERMANENT,
                        DeliveryStatus.RECIPIENT_REJECTED,
                        DeliveryStatus.MAILBOX_FULL,
                        DeliveryStatus.DOMAIN_REJECTED,
                        DeliveryStatus.POLICY_REJECTED,
                        DeliveryStatus.SPAM_REJECTED,
                        DeliveryStatus.DELIVERY_FAILED,
                    ]
                ),
            )
        )
        or 0
    )
    scan_errors = sum(scan.parsing_errors + scan.network_errors for scan in scans)
    matching_metrics = await _daily_matching_metrics(
        session,
        start,
        end,
        profile_id=report_profile_id,
    )
    external_metrics = await external_call_metrics(session, start, end)
    limit_metrics = await _daily_limit_metrics(session, start, end)
    if session.get_bind().dialect.name == "sqlite":
        minimum_diagnostics = await _daily_minimum_diagnostics(
            session,
            start,
            end,
            profile_id=report_profile.id if report_profile is not None else None,
            minimum=int(limit_metrics.get("daily_effective_minimum") or 0),
            sent=int(limit_metrics.get("daily_sent") or 0),
        )
        minimum_diagnostics["daily_minimum_diagnostics_state"] = "ok"
    else:
        minimum_diagnostics = await _bounded_minimum_diagnostics(
            start,
            end,
            profile_id=report_profile.id if report_profile is not None else None,
            minimum=int(limit_metrics.get("daily_effective_minimum") or 0),
            sent=int(limit_metrics.get("daily_sent") or 0),
        )
    minimum = int(limit_metrics.get("daily_effective_minimum") or 0)
    sent = int(limit_metrics.get("daily_sent") or 0)
    reserved = int(minimum_diagnostics.get("safe_auto_send_ready") or 0)
    search_active = minimum > 0 and sent + reserved < minimum
    limit_metrics["daily_minimum_reserved"] = reserved
    limit_metrics["daily_minimum_search_active"] = search_active
    limit_metrics["daily_minimum_effective_score"] = (
        limit_metrics["daily_minimum_catchup_stages"][-1]
        if search_active
        else limit_metrics["daily_minimum_normal_score"]
    )
    matching_backlog: int | None
    if session.get_bind().dialect.name == "sqlite":
        matching_backlog = await _profile_matching_backlog(session, report_profile_id)
        matching_backlog_state = "ok"
    else:
        matching_backlog, matching_backlog_state = await _bounded_matching_backlog(
            report_profile_id
        )
    active_jobs = int(
        await session.scalar(
            select(func.count(SourceJob.id)).where(SourceJob.status == JobStatus.ACTIVE)
        )
        or 0
    )
    phone_metrics = await daily_phone_metrics(session, start, end)
    delivery_event_at = func.coalesce(
        EmailDelivery.bounced_at,
        EmailDelivery.last_attempt_at,
        EmailDelivery.submitted_at,
    )
    delivery_rows = (
        await session.execute(
            select(
                EmailDelivery.status,
                EmailDelivery.smtp_status,
                EmailDelivery.failure_class,
                func.count(EmailDelivery.id),
            )
            .where(delivery_event_at >= start, delivery_event_at < end)
            .group_by(
                EmailDelivery.status,
                EmailDelivery.smtp_status,
                EmailDelivery.failure_class,
            )
        )
    ).all()
    delivery_status_counts: dict[str, int] = {}
    permanent_breakdown: dict[str, int] = {}
    for status, smtp_status, failure_class, count in delivery_rows:
        delivery_status_counts[status.value] = delivery_status_counts.get(status.value, 0) + int(
            count
        )
        if status in {
            DeliveryStatus.BOUNCED_PERMANENT,
            DeliveryStatus.RECIPIENT_REJECTED,
            DeliveryStatus.DOMAIN_REJECTED,
            DeliveryStatus.POLICY_REJECTED,
            DeliveryStatus.SPAM_REJECTED,
            DeliveryStatus.PERMANENT_FAILURE,
        }:
            key = f"{smtp_status or 'unknown'}:{failure_class or 'other'}"
            permanent_breakdown[key] = permanent_breakdown.get(key, 0) + int(count)
    submitted_count = int(
        await session.scalar(
            select(func.count(EmailDelivery.id)).where(
                EmailDelivery.submitted_at >= start,
                EmailDelivery.submitted_at < end,
            )
        )
        or 0
    )
    provider_accepted_count = int(
        await session.scalar(
            select(func.count(EmailDelivery.id)).where(
                EmailDelivery.provider_accepted_at >= start,
                EmailDelivery.provider_accepted_at < end,
            )
        )
        or 0
    )
    # Current outcome of the messages first handed to the provider today. The
    # initial acceptance is a durable fact; the current status may change later
    # through a DSN (e.g. 21 accepted = 20 still accepted + 1 domain_rejected).
    submitted_cohort_rows = (
        await session.execute(
            select(EmailDelivery.status, func.count(EmailDelivery.id))
            .where(EmailDelivery.submitted_at >= start, EmailDelivery.submitted_at < end)
            .group_by(EmailDelivery.status)
        )
    ).all()
    submitted_cohort_current = {status.value: int(count) for status, count in submitted_cohort_rows}
    provider_submissions = int(
        await session.scalar(
            select(func.count(EmailSendAttempt.id)).where(
                EmailSendAttempt.local_day == local_day_key(start),
                EmailSendAttempt.outcome.in_(COUNTED_OUTCOMES),
            )
        )
        or 0
    )
    permanent_count = sum(permanent_breakdown.values())
    # Messages the provider is still retrying after warning about a delay; the
    # final outcome arrives only when its retry window (about three days) ends.
    delayed_rows = (
        await session.execute(
            select(EmailDelivery.recipient, EmailDelivery.smtp_status)
            .where(
                EmailDelivery.failure_class == "delivery_delayed",
                EmailDelivery.status.in_(
                    [
                        DeliveryStatus.SUBMITTED,
                        DeliveryStatus.PROVIDER_ACCEPTED,
                        DeliveryStatus.SENT,
                    ]
                ),
            )
            .order_by(EmailDelivery.updated_at)
        )
    ).all()
    permanent_statuses = {
        DeliveryStatus.BOUNCED_PERMANENT,
        DeliveryStatus.RECIPIENT_REJECTED,
        DeliveryStatus.DOMAIN_REJECTED,
        DeliveryStatus.POLICY_REJECTED,
        DeliveryStatus.SPAM_REJECTED,
        DeliveryStatus.PERMANENT_FAILURE,
    }
    permanent_cohort_count = int(
        await session.scalar(
            select(func.count(EmailDelivery.id)).where(
                EmailDelivery.submitted_at >= start,
                EmailDelivery.submitted_at < end,
                EmailDelivery.status.in_(permanent_statuses),
            )
        )
        or 0
    )
    delivery_alerts: list[dict[str, object]] = []
    if submitted_count >= 10 and permanent_cohort_count / submitted_count > 0.10:
        delivery_alerts.append(
            {
                "severity": "warning",
                "code": "email_permanent_bounce_rate",
                "permanent_bounces": permanent_cohort_count,
                "submitted": submitted_count,
                "rate": round(permanent_cohort_count / submitted_count, 4),
            }
        )
    auth_failures = sum(
        count
        for key, count in permanent_breakdown.items()
        if key.endswith(":authentication_failure")
    )
    if auth_failures:
        delivery_alerts.append(
            {
                "severity": "high",
                "code": "email_authentication_failures",
                "count": auth_failures,
            }
        )
    permanent_deliveries = list(
        (
            await session.scalars(
                select(EmailDelivery).where(
                    delivery_event_at >= start,
                    delivery_event_at < end,
                    EmailDelivery.status.in_(permanent_statuses),
                )
            )
        ).all()
    )
    rejected_by_domain: dict[str, int] = {}
    for delivery in permanent_deliveries:
        domain = delivery.recipient.rsplit("@", maxsplit=1)[-1].casefold()
        rejected_by_domain[domain] = rejected_by_domain.get(domain, 0) + 1
    for domain, count in sorted(rejected_by_domain.items()):
        if count >= 3:
            delivery_alerts.append(
                {
                    "severity": "warning",
                    "code": f"email_domain_mass_rejection:{domain}"[:128],
                    "domain": domain,
                    "count": count,
                }
            )
    policy_failures = sum(
        delivery.status in {DeliveryStatus.POLICY_REJECTED, DeliveryStatus.SPAM_REJECTED}
        for delivery in permanent_deliveries
    )
    if policy_failures >= 3:
        delivery_alerts.append(
            {
                "severity": "high",
                "code": "email_policy_rejection_spike",
                "count": policy_failures,
            }
        )
    retry_stuck = int(
        await session.scalar(
            select(func.count(EmailDelivery.id)).where(
                EmailDelivery.status.in_(
                    {DeliveryStatus.BOUNCED_TRANSIENT, DeliveryStatus.TEMPORARY_FAILURE}
                ),
                EmailDelivery.next_retry_at < datetime.now(UTC),
            )
        )
        or 0
    )
    if retry_stuck >= 3:
        delivery_alerts.append(
            {
                "severity": "warning",
                "code": "email_retry_queue_stuck",
                "count": retry_stuck,
            }
        )
    if persist:
        for alert_data in delivery_alerts:
            code = str(alert_data["code"])
            existing_alert = await session.scalar(
                select(Alert.id).where(
                    Alert.code == code,
                    Alert.created_at >= start,
                    Alert.acknowledged.is_(False),
                )
            )
            if existing_alert is None:
                session.add(
                    Alert(
                        severity=str(alert_data["severity"]),
                        code=code,
                        message="Email delivery health requires operator attention",
                        safe_diagnostics=alert_data,
                        acknowledged=False,
                    )
                )
    relationship_applications = list(
        (
            await session.scalars(
                select(Application).where(
                    Application.created_at >= start,
                    Application.created_at < end,
                )
            )
        ).all()
    )
    suppressed_applications = sum(
        bool(
            {"employer_not_suppressed", "no_candidate_withdrawal"}
            & {
                item
                for item in (application.policy_result or {}).get("rules_failed", [])
                if isinstance(item, str)
            }
        )
        for application in relationship_applications
    )
    active_employers = int(
        await session.scalar(
            select(func.count(EmployerRelationship.id)).where(
                EmployerRelationship.state.in_(
                    {
                        EmployerRelationshipState.APPLICATION_ACTIVE,
                        EmployerRelationshipState.EMPLOYER_REPLIED,
                        EmployerRelationshipState.INTERVIEW_PENDING,
                        EmployerRelationshipState.INTERVIEWED,
                    }
                )
            )
        )
        or 0
    )
    relationship_review_count = int(
        await session.scalar(
            select(func.count(EmployerIdentityCandidate.id)).where(
                EmployerIdentityCandidate.status == "needs_review"
            )
        )
        or 0
    )
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
        "matching_scope": "default_profile",
        "external_calls": external_metrics,
        "external_calls_scope": "system",
        "phone_calls": phone_metrics,
        "employer_safety": {
            "employers_with_active_conversation": active_employers,
            "employer_suppressed_applications": suppressed_applications,
            "same_employer_applications_deferred": sum(
                application.status is ApplicationStatus.DEFERRED
                for application in relationship_applications
            ),
            "relationship_review_count": relationship_review_count,
        },
        "email_delivery": {
            # Hard-maximum ledger: provider submissions of this local day that
            # consume the maximum (accepted, in-flight, unknown), all profiles.
            "provider_submissions": provider_submissions,
            # First provider acceptance recorded today (durable, not undone by a bounce).
            "initially_accepted": provider_accepted_count,
            # Current delivery status of the cohort first submitted today.
            "submitted_cohort_current_status": submitted_cohort_current,
            "submitted": submitted_count,
            "provider_accepted": provider_accepted_count,
            "delivery_unknown": delivery_status_counts.get("delivery_unknown", 0),
            "bounced_transient": delivery_status_counts.get("bounced_transient", 0),
            "bounced_permanent": permanent_count,
            "permanent_bounce_events": permanent_count,
            "submitted_cohort_permanent_failures": permanent_cohort_count,
            "known_permanent_bounce_rate": (
                round(permanent_cohort_count / submitted_count, 4) if submitted_count else 0.0
            ),
            "by_status": delivery_status_counts,
            "permanent_failure_breakdown": permanent_breakdown,
            "delayed_in_flight": len(delayed_rows),
            "delayed_recipients": [
                {"recipient": recipient, "smtp_status": smtp_status}
                for recipient, smtp_status in delayed_rows
            ],
            "alerts": delivery_alerts,
        },
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
        **minimum_diagnostics,
        "active_jobs": active_jobs,
        "matching_backlog": matching_backlog,
        "matching_backlog_state": matching_backlog_state,
    }
    from app.learning.shadow import shadow_scorecard

    summary["learning_shadow"] = [
        await shadow_scorecard(session, profile.id)
        for profile in await ProfileService().list_processing_profiles(session)
    ]
    summary["finalized"] = finalized
    summary["target_policy_version"] = TARGET_POLICY_VERSION
    summary["collection_started_at"] = collection_started_at.isoformat()
    summary["snapshot_at"] = datetime.now(UTC).isoformat()
    summary["scopes"] = {
        "matching": "default_profile",
        "daily_limit": "default_profile",
        "daily_minimum": "default_profile",
        "daily_targets": "per_profile",
        "external_calls": "system",
        "email_delivery": "system",
        "sent_applications": "system",
    }
    summary["counter_definitions"] = REPORT_COUNTER_DEFINITIONS
    summary["local_date"] = start_local.date().isoformat()
    from app.applications.diagnostics import daily_minimum_audit

    summary["daily_targets"] = [
        await daily_minimum_audit(session, preference.profile_id, day=start_local.date())
        for preference in (await session.scalars(select(JobPreference))).all()
    ]
    recent_reports = list(
        (
            await session.scalars(
                select(DailyReport).where(
                    DailyReport.report_date >= start - timedelta(days=30),
                    DailyReport.report_date < start,
                )
            )
        ).all()
    )
    completed_targets = [
        target
        for report in recent_reports
        if report.summary.get("finalized")
        for target in report.summary.get("daily_targets", [])
        if target.get("minimum", 0) > 0
    ]
    summary["minimum_completion"] = {
        "observed_profile_days": len(completed_targets),
        "met_profile_days": sum(
            target["sent"] >= target["minimum"] for target in completed_targets
        ),
        "deficit_sends": sum(
            max(0, target["minimum"] - target["sent"]) for target in completed_targets
        ),
    }
    if not persist:
        return DailyReport(report_date=start, summary=summary)

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
        current_local, _, _ = local_day_bounds()
        report = await _generate(session, day=current_local.date() - timedelta(days=1))
        await session.commit()
        return dict(report.summary)
