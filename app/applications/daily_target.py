from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawlers.parsing.normalization import normalize_for_fingerprint
from app.models.entities import Application, EmailDelivery, JobPreference, MatchEvaluation
from app.models.enums import ApplicationStatus, DeliveryStatus, MatchDecision
from app.time_utils import local_day_bounds

TARGET_POLICY_VERSION = "2026-09-30-distinct-employer-catchup"


def minimum_daily_requirement(preference: JobPreference) -> int:
    rules = preference.additional_rules or {}
    try:
        minimum_daily = int(rules.get("minimum_daily_applications", 0))
    except (TypeError, ValueError):
        return 0
    return max(0, min(minimum_daily, preference.maximum_daily_applications))


def minimum_catchup_score(preference: JobPreference) -> int:
    """Return the bounded soft auto-apply threshold while minimum is unmet."""

    rules = preference.additional_rules or {}
    try:
        delta = int(rules.get("minimum_daily_catchup_score_delta", 10))
    except (TypeError, ValueError):
        delta = 10
    delta = max(0, min(delta, 20))
    return max(0, preference.minimum_auto_send_score - delta)


def minimum_catchup_active(preference: JobPreference, sent_today: int) -> bool:
    minimum = minimum_daily_requirement(preference)
    return minimum > 0 and sent_today < minimum


def minimum_catchup_scores(preference: JobPreference) -> tuple[int, ...]:
    """Best matches first; aggressive stages are used only for the missing sends."""
    first = minimum_catchup_score(preference)
    floor = min(first, 40)
    return tuple(dict.fromkeys((*range(first, floor - 1, -10), floor)))


def catchup_stage(evaluation: MatchEvaluation, preference: JobPreference) -> int | None:
    for threshold in minimum_catchup_scores(preference):
        if review_is_safe_catchup_candidate(evaluation, threshold=threshold, allow_soft_skip=True):
            return threshold
    return None


def review_is_safe_catchup_candidate(
    evaluation: MatchEvaluation,
    *,
    threshold: int,
    allow_soft_skip: bool = False,
) -> bool:
    """Allow catch-up promotion only for an already LLM-reviewed soft-borderline match."""

    material_risks = [
        risk for risk in (evaluation.risks or []) if risk != "experience_relevance_requires_review"
    ]
    return (
        (
            evaluation.decision in {MatchDecision.PREPARE_FOR_REVIEW, MatchDecision.AUTO_APPLY}
            or (
                allow_soft_skip
                and evaluation.decision is MatchDecision.SKIP
                and bool(evaluation.soft_mismatches)
            )
        )
        and evaluation.overall_fit >= threshold
        and not evaluation.missing_requirements
        and len(evaluation.optional_requirements_missing or []) <= 1
        and not material_risks
        and not evaluation.scam_indicators
    )


@dataclass(frozen=True)
class DailyTargetState:
    day: str
    minimum: int
    maximum: int
    sent: int
    reserved: int
    sent_employers: frozenset[UUID]
    reserved_employers: frozenset[UUID]
    sent_companies: frozenset[str]
    reserved_companies: frozenset[str]

    @property
    def remaining(self) -> int:
        return max(0, self.minimum - self.sent - self.reserved)


async def lock_daily_target(session: AsyncSession) -> None:
    """Share the sender's transaction lock for quota checks and reservation creation."""
    if session.get_bind().dialect.name == "postgresql":
        local_start, _, _ = local_day_bounds()
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:quota_lock))"),
            {"quota_lock": f"job-agent:email-daily:{local_start.date().isoformat()}"},
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def daily_target_state(
    session: AsyncSession,
    preference: JobPreference,
    *,
    exclude_application_id: UUID | None = None,
    now: datetime | None = None,
) -> DailyTargetState:
    """Count confirmed sends and current, usable reservations, once per employer.

    An in-flight provider attempt consumes capacity even if a preference has
    changed. Uncertain delivery is never considered a confirmed minimum send.
    Old approvals must pass policy again before reserving a new local day.
    """
    from app.contacts import contact_is_source_verified
    from app.employers import EmployerRelationshipService
    from app.matching.bindings import evaluation_inputs_are_current
    from app.matching.freshness import evaluation_is_current
    from app.models.entities import EmployerContact, JobSource, Resume, SourceJob, UserProfile
    from app.models.enums import ContactDeliveryState, JobStatus, SourceHealth
    from app.profiles.source_categories import category_policy
    from app.profiles.sources import source_selected
    from app.settings import get_settings

    unanswered_release_days = get_settings().employer_unanswered_release_days
    local_start, start, end = local_day_bounds(now=now)
    day = local_start.date().isoformat()
    applications = list(
        (
            await session.scalars(
                select(Application).where(
                    Application.profile_id == preference.profile_id,
                    or_(
                        and_(
                            Application.status == ApplicationStatus.SENT,
                            Application.sent_at >= start,
                            Application.sent_at < end,
                        ),
                        Application.status.in_(
                            {
                                ApplicationStatus.AUTO_APPROVED,
                                ApplicationStatus.SENDING,
                                ApplicationStatus.DELIVERY_UNKNOWN,
                            }
                        ),
                    ),
                )
            )
        ).all()
    )
    company_names: dict[UUID, str | None] = (
        {
            job_id: company
            for job_id, company in (
                await session.execute(
                    select(SourceJob.id, SourceJob.company).where(
                        SourceJob.id.in_(
                            [application.source_job_id for application in applications]
                        ),
                    )
                )
            ).all()
        }
        if applications
        else {}
    )
    applications.sort(key=lambda application: application.status is not ApplicationStatus.SENT)
    deliveries_by_application: dict[UUID, list[EmailDelivery]] = {}
    if applications:
        for delivery in (
            await session.scalars(
                select(EmailDelivery).where(
                    EmailDelivery.application_id.in_(
                        [application.id for application in applications]
                    ),
                )
            )
        ).all():
            deliveries_by_application.setdefault(delivery.application_id, []).append(delivery)
    sent = 0
    sent_employers: set[UUID] = set()
    reserved_employers: set[UUID] = set()
    sent_companies: set[str] = set()
    reserved_companies: set[str] = set()
    profile = await session.get(UserProfile, preference.profile_id)
    for application in applications:
        company_key = normalize_for_fingerprint(company_names.get(application.source_job_id))
        deliveries = deliveries_by_application.get(application.id, [])
        permanently_failed = any(
            delivery.status
            in {
                DeliveryStatus.BOUNCED_PERMANENT,
                DeliveryStatus.RECIPIENT_REJECTED,
                DeliveryStatus.DOMAIN_REJECTED,
                DeliveryStatus.POLICY_REJECTED,
                DeliveryStatus.SPAM_REJECTED,
                DeliveryStatus.PERMANENT_FAILURE,
                DeliveryStatus.DELIVERY_FAILED,
            }
            for delivery in deliveries
        )
        if (
            application.status is ApplicationStatus.SENT
            and application.sent_at is not None
            and start <= _aware(application.sent_at) < end
            and not permanently_failed
        ):
            sent += 1
            if application.employer_id is not None:
                sent_employers.add(application.employer_id)
            if company_key:
                sent_companies.add(company_key)
            continue
        if application.id == exclude_application_id or application.employer_id is None:
            continue
        if application.status is ApplicationStatus.DELIVERY_UNKNOWN:
            # Unknown delivery freezes that company, but cannot satisfy a target
            # requiring confirmed sends or an actually ready reservation.
            if company_key:
                reserved_companies.add(company_key)
            continue
        if application.status in {ApplicationStatus.SENDING, ApplicationStatus.DELIVERY_UNKNOWN}:
            if any(
                start <= _aware(delivery.last_attempt_at or delivery.created_at) < end
                and delivery.status
                in {
                    DeliveryStatus.SENDING,
                    DeliveryStatus.DELIVERY_UNKNOWN,
                    DeliveryStatus.PROVIDER_ACCEPTED,
                }
                for delivery in deliveries
            ):
                reserved_employers.add(application.employer_id)
                if company_key:
                    reserved_companies.add(company_key)
            continue
        result = application.policy_result or {}
        if (
            application.status is not ApplicationStatus.AUTO_APPROVED
            or permanently_failed
            or result.get("target_reservation_day") != day
            or result.get("target_policy_version") != TARGET_POLICY_VERSION
            or result.get("rules_failed")
            or not application.content_validated
            or not preference.auto_send_enabled
            or preference.global_pause
        ):
            continue
        evaluation = await session.get(MatchEvaluation, application.match_evaluation_id)
        job = await session.get(SourceJob, application.source_job_id)
        resume = await session.get(Resume, application.resume_id)
        contact = await session.get(EmployerContact, application.recipient_contact_id)
        source = await session.get(JobSource, job.source_id) if job is not None else None
        if (
            profile is None
            or evaluation is None
            or job is None
            or resume is None
            or contact is None
            or source is None
            or not source.enabled
            or source.automatic_actions_paused
            or source.health_status is not SourceHealth.HEALTHY
            or job.status is not JobStatus.ACTIVE
            or not resume.active
            or not resume.verified
            or not contact_is_source_verified(contact)
            or contact.delivery_state
            in {
                ContactDeliveryState.INVALID,
                ContactDeliveryState.REJECTED,
                ContactDeliveryState.SUPPRESSED,
            }
            or (job.category or "").casefold()
            not in {
                value.casefold()
                for value in (await category_policy(session, preference, job.source_id)).auto_send
            }
            or not evaluation_inputs_are_current(evaluation, profile, preference, resume)
            or not await evaluation_is_current(session, evaluation, job)
            or not await source_selected(session, profile.id, source.id)
        ):
            continue
        outcome = await EmployerRelationshipService().policy_outcome(
            session,
            application=application,
            evaluation=evaluation,
            job=job,
            max_active_applications=1,
            freeze_active_conversation=True,
            unanswered_release_days=unanswered_release_days,
        )
        if not outcome.slot_available or not outcome.not_suppressed:
            continue
        if company_key and company_key in (sent_companies | reserved_companies):
            continue
        reserved_employers.add(application.employer_id)
        if company_key:
            reserved_companies.add(company_key)
    reserved_employers -= sent_employers
    return DailyTargetState(
        day,
        minimum_daily_requirement(preference),
        preference.maximum_daily_applications,
        sent,
        len(reserved_employers),
        frozenset(sent_employers),
        frozenset(reserved_employers),
        frozenset(sent_companies),
        frozenset(reserved_companies),
    )
