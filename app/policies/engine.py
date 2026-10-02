from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.daily_target import (
    TARGET_POLICY_VERSION,
    catchup_stage,
    daily_target_state,
    lock_daily_target,
)
from app.contacts import contact_is_source_verified
from app.crawlers.parsing.normalization import (
    detect_prompt_injection,
    detect_scam_indicators,
    normalize_for_fingerprint,
)
from app.delivery_ledger import transmissions_on_day
from app.employers import EmployerIdentityService, EmployerRelationshipService
from app.matching.hard_requirements import (
    HARD_REQUIREMENT_RULES_VERSION,
    HardRequirementEngine,
    all_hard_requirements_met,
    hard_requirements_snapshot,
)
from app.matching.schemas import HardRequirementStatus
from app.models.entities import (
    Application,
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
    ContactDeliveryState,
    ContactType,
    DeliveryStatus,
    JobStatus,
    MatchDecision,
    PolicyDecision,
    SourceHealth,
)
from app.observability.metrics import (
    APPLICATIONS_BLOCKED_EMPLOYER_SUPPRESSION,
    APPLICATIONS_DEFERRED_SAME_EMPLOYER,
)
from app.policies.schemas import PolicyResult
from app.profiles.source_categories import category_policy
from app.profiles.sources import source_selected
from app.settings import Settings

POLICY_VERSION = "2026-10-02.2-employer-release-daily-capacity"
NO_PUBLIC_EMAIL_STOP_REASON = "no_public_email"
DAILY_LIMIT_STOP_REASON = "daily_limit_reached"


class PolicyEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.employer_relationships = EmployerRelationshipService()

    async def evaluate(
        self,
        session: AsyncSession,
        application: Application,
        preferences: JobPreference,
        evaluation: MatchEvaluation,
        job: SourceJob,
        resume: Resume,
        contact: EmployerContact,
        profile: UserProfile,
    ) -> PolicyResult:
        await session.execute(
            select(Application.id).where(Application.id == application.id).with_for_update()
        )
        await lock_daily_target(session)
        passed: list[str] = []
        failed: list[str] = []

        def rule(name: str, condition: bool) -> None:
            (passed if condition else failed).append(name)

        source = await session.get(JobSource, job.source_id)
        if job.employer_id is None:
            identity = await EmployerIdentityService().resolve_for_source_job(session, job)
        else:
            identity = None
        if application.employer_id is None and job.employer_id is not None:
            application.employer_id = job.employer_id
        if contact.employer_id is None and job.employer_id is not None:
            contact.employer_id = job.employer_id
        if identity is not None and application.employer_id is None:
            application.employer_id = identity.employer.id
        if identity is not None and contact.employer_id is None:
            contact.employer_id = identity.employer.id
        category = (job.category or "").casefold()
        source_categories = await category_policy(session, preferences, job.source_id)
        auto_categories = {item.casefold() for item in source_categories.auto_send}
        additional_rules = preferences.additional_rules or {}
        raw_forbidden_title_terms = additional_rules.get("forbidden_title_terms", [])
        forbidden_title_terms = (
            [
                normalize_for_fingerprint(item)
                for item in raw_forbidden_title_terms
                if isinstance(item, str) and normalize_for_fingerprint(item)
            ]
            if isinstance(raw_forbidden_title_terms, list)
            else []
        )
        normalized_title = normalize_for_fingerprint(job.title)
        title_tokens = set(normalized_title.split())
        title_forbidden = any(
            term == normalized_title or set(term.split()) <= title_tokens
            for term in forbidden_title_terms
        )
        confirmed = {
            str(item.get("id") or item.get("statement") or item.get("text"))
            for item in profile.confirmed_facts
            if item.get("confirmed") is True
        }
        used_facts = set(application.used_confirmed_facts)
        untrusted_text = "\n".join(
            value
            for value in (
                job.title,
                job.company,
                job.description,
                job.requirements,
                job.responsibilities,
            )
            if value
        )
        # Hard maximum: provider submissions of this local day, including accepted
        # messages that bounced later, in-flight reservations and unknown outcomes.
        attempts_today = await transmissions_on_day(session, profile_id=application.profile_id)
        target = await daily_target_state(
            session, preferences, exclude_application_id=application.id
        )
        stage = catchup_stage(evaluation, preferences) if target.remaining > 0 else None
        normal_match = (
            evaluation.decision is MatchDecision.AUTO_APPLY
            and evaluation.overall_fit >= preferences.minimum_auto_send_score
        )
        if normal_match:
            stage = None
        catchup_promotion = stage is not None
        effective_auto_send_score = (
            stage if stage is not None else preferences.minimum_auto_send_score
        )
        prior_unknown = await session.scalar(
            select(func.count(EmailDelivery.id)).where(
                EmailDelivery.application_id == application.id,
                EmailDelivery.status == DeliveryStatus.DELIVERY_UNKNOWN,
            )
        )
        current_hard_requirements = HardRequirementEngine().evaluate(job, profile)
        current_hard_snapshot = hard_requirements_snapshot(current_hard_requirements)
        hard_requirements_met = all_hard_requirements_met(current_hard_requirements)
        hard_requirement_binding_current = not current_hard_requirements or (
            evaluation.hard_requirement_rules_version == HARD_REQUIREMENT_RULES_VERSION
            and (evaluation.hard_requirements or []) == current_hard_snapshot
        )
        hard_requirement_missing = any(
            item.status is HardRequirementStatus.MISSING for item in current_hard_requirements
        )
        hard_requirement_unknown = any(
            item.status is HardRequirementStatus.UNKNOWN for item in current_hard_requirements
        )
        employer_policy = await self.employer_relationships.policy_outcome(
            session,
            application=application,
            evaluation=evaluation,
            job=job,
            max_active_applications=1,
            freeze_active_conversation=True,
            unanswered_release_days=self.settings.employer_unanswered_release_days,
        )

        rule("deployment_emergency_switch_off", not self.settings.emergency_email_kill_switch)
        rule("auto_send_enabled", preferences.auto_send_enabled)
        rule("global_pause_off", not preferences.global_pause)
        rule("source_healthy", bool(source and source.health_status == SourceHealth.HEALTHY))
        rule(
            "source_selected_for_profile",
            await source_selected(session, profile.id, job.source_id),
        )
        rule(
            "source_actions_enabled",
            bool(source and source.enabled and not source.automatic_actions_paused),
        )
        rule("category_allowed_for_auto_send", category in auto_categories)
        rule("job_title_allowed_by_preferences", not title_forbidden)
        rule(
            "overall_score_threshold",
            evaluation.overall_fit >= effective_auto_send_score,
        )
        rule("mandatory_requirements_met", not evaluation.missing_requirements)
        rule(
            "no_material_match_risk",
            not [
                risk
                for risk in (evaluation.risks or [])
                if risk != "experience_relevance_requires_review"
            ],
        )
        rule("deterministic_hard_requirements_met", hard_requirements_met)
        rule("hard_requirement_binding_current", hard_requirement_binding_current)
        rule(
            "match_not_blocked",
            evaluation.decision != MatchDecision.BLOCK,
        )
        rule(
            "match_not_skipped",
            evaluation.decision != MatchDecision.SKIP or catchup_promotion,
        )
        rule(
            "match_auto_apply",
            evaluation.decision == MatchDecision.AUTO_APPLY or catchup_promotion,
        )
        rule("verified_email_contact", contact.contact_type == ContactType.EMAIL)
        rule("contact_verified", contact_is_source_verified(contact))
        rule(
            "contact_delivery_usable",
            contact.delivery_state
            not in {
                ContactDeliveryState.INVALID,
                ContactDeliveryState.REJECTED,
                ContactDeliveryState.SUPPRESSED,
            },
        )
        rule(
            "contact_same_employer",
            application.employer_id is not None
            and application.employer_id == job.employer_id == contact.employer_id,
        )
        rule("employer_identity_resolved", employer_policy.employer_resolved)
        rule("employer_not_suppressed", employer_policy.not_suppressed)
        rule("no_candidate_withdrawal", employer_policy.no_candidate_withdrawal)
        rule("no_active_employer_conversation", employer_policy.no_active_conversation)
        rule("employer_application_slot_available", employer_policy.slot_available)
        company_key = normalize_for_fingerprint(job.company)
        distinct_company = application.employer_id not in target.sent_employers and (
            not company_key or company_key not in target.sent_companies | target.reserved_companies
        )
        rule("distinct_employer_today", distinct_company)
        rule("vacancy_active", job.status == JobStatus.ACTIVE)
        rule(
            "profile_binding_valid",
            application.profile_id
            == profile.id
            == preferences.profile_id
            == evaluation.profile_id
            == resume.profile_id,
        )
        rule("resume_active_verified", resume.active and resume.verified)
        rule("letter_validated", application.content_validated)
        rule("all_claims_confirmed", used_facts <= confirmed)
        rule("no_prompt_injection", not detect_prompt_injection(untrusted_text))
        rule("no_deterministic_scam_pattern", not detect_scam_indicators(untrusted_text))
        rule("no_scam_indicators", not evaluation.scam_indicators)
        rule(
            "not_previously_sent",
            application.status not in {ApplicationStatus.SENT, ApplicationStatus.SENDING},
        )
        rule("no_delivery_unknown", not prior_unknown)
        # A new approval also competes with applications already approved for
        # today, so the policy never approves more than can still be sent.
        # An application that already holds an approval is checked against
        # confirmed submissions only: the sender re-evaluates it right before
        # sending, and the other reservations must not block that.
        holds_approval = application.status in {
            ApplicationStatus.AUTO_APPROVED,
            ApplicationStatus.APPROVED,
            ApplicationStatus.SENDING,
            ApplicationStatus.FAILED,
        }
        reserved_today = 0 if holds_approval else target.reserved
        rule(
            "daily_limit",
            int(attempts_today or 0) + reserved_today < preferences.maximum_daily_applications,
        )

        hard_block_rules = {
            "deployment_emergency_switch_off",
            "source_healthy",
            "source_selected_for_profile",
            "source_actions_enabled",
            "job_title_allowed_by_preferences",
            "match_not_blocked",
            "vacancy_active",
            "profile_binding_valid",
            "all_claims_confirmed",
            "no_prompt_injection",
            "no_deterministic_scam_pattern",
            "no_scam_indicators",
            "not_previously_sent",
            "no_delivery_unknown",
            "contact_delivery_usable",
            "contact_same_employer",
            "employer_not_suppressed",
            "no_candidate_withdrawal",
        }
        if hard_block_rules & set(failed):
            decision = PolicyDecision.BLOCKED
        elif "verified_email_contact" in failed:
            # JobHunter never submits through a job board's internal form, so
            # there is nothing the owner could approve and no employer slot
            # worth waiting for.
            decision = PolicyDecision.SKIPPED
        elif {
            "no_active_employer_conversation",
            "employer_application_slot_available",
            "distinct_employer_today",
        } & set(failed):
            decision = PolicyDecision.DEFERRED
        elif hard_requirement_missing:
            decision = PolicyDecision.SKIPPED
        elif hard_requirement_unknown or not hard_requirement_binding_current:
            decision = PolicyDecision.PENDING_REVIEW
        elif evaluation.decision == MatchDecision.SKIP and not catchup_promotion:
            decision = PolicyDecision.SKIPPED
        elif failed == ["daily_limit"]:
            # Nothing for the owner to decide: it waits for capacity.
            decision = PolicyDecision.DEFERRED
        elif failed:
            decision = PolicyDecision.PENDING_REVIEW
        else:
            decision = PolicyDecision.AUTO_APPROVED
        return PolicyResult(
            decision=decision,
            rules_passed=passed,
            rules_failed=failed,
            policy_version=POLICY_VERSION,
            hard_safety_passed=not (
                set(failed)
                & (
                    hard_block_rules
                    | {
                        "mandatory_requirements_met",
                        "deterministic_hard_requirements_met",
                        "hard_requirement_binding_current",
                        "no_material_match_risk",
                    }
                )
            ),
            content_ready=not (
                set(failed)
                & {
                    "resume_active_verified",
                    "verified_email_contact",
                    "contact_verified",
                    "letter_validated",
                    "profile_binding_valid",
                    "all_claims_confirmed",
                }
            ),
            employer_slot_available=employer_policy.slot_available and distinct_company,
            soft_match_passed=normal_match,
            catchup_stage=stage,
            minimum_remaining=target.remaining,
            target_reservation_day=target.day,
            target_policy_version=TARGET_POLICY_VERSION,
        )

    async def apply(
        self,
        session: AsyncSession,
        application: Application,
        preferences: JobPreference,
        evaluation: MatchEvaluation,
        job: SourceJob,
        resume: Resume,
        contact: EmployerContact,
        profile: UserProfile,
    ) -> PolicyResult:
        result = await self.evaluate(
            session, application, preferences, evaluation, job, resume, contact, profile
        )
        application.policy_decision = result.decision
        failed = set(result.rules_failed)
        policy_result = result.model_dump(mode="json")
        if result.decision is PolicyDecision.DEFERRED:
            if failed == {"daily_limit"}:
                reason = DAILY_LIMIT_STOP_REASON
            elif "no_active_employer_conversation" in failed:
                reason = "active_employer_conversation"
            else:
                reason = "same_employer_application_deferred"
            policy_result.update(
                {
                    "safe_stop_reason": reason,
                    "requires_rematch": False,
                }
            )
        elif result.decision is PolicyDecision.SKIPPED and "verified_email_contact" in failed:
            policy_result.update(
                {
                    "safe_stop_reason": NO_PUBLIC_EMAIL_STOP_REASON,
                    "requires_rematch": False,
                }
            )
        application.policy_result = policy_result
        status_map = {
            PolicyDecision.AUTO_APPROVED: ApplicationStatus.AUTO_APPROVED,
            PolicyDecision.PENDING_REVIEW: ApplicationStatus.PENDING_REVIEW,
            PolicyDecision.DEFERRED: ApplicationStatus.DEFERRED,
            PolicyDecision.BLOCKED: ApplicationStatus.BLOCKED,
            PolicyDecision.SKIPPED: ApplicationStatus.CANCELLED,
        }
        application.status = status_map[result.decision]
        if result.decision is PolicyDecision.DEFERRED and failed != {"daily_limit"}:
            APPLICATIONS_DEFERRED_SAME_EMPLOYER.inc()
        if {"employer_not_suppressed", "no_candidate_withdrawal"} & failed:
            APPLICATIONS_BLOCKED_EMPLOYER_SUPPRESSION.inc()
        await session.flush()
        return result
