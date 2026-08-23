from __future__ import annotations

from datetime import UTC, datetime, time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawlers.parsing.normalization import (
    detect_prompt_injection,
    detect_scam_indicators,
    normalize_for_fingerprint,
)
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
    ContactType,
    DeliveryStatus,
    JobStatus,
    MatchDecision,
    PolicyDecision,
    SourceHealth,
    VerificationStatus,
)
from app.policies.schemas import PolicyResult
from app.settings import Settings

POLICY_VERSION = "2026-08-03.1"


class PolicyEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

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
        passed: list[str] = []
        failed: list[str] = []

        def rule(name: str, condition: bool) -> None:
            (passed if condition else failed).append(name)

        source = await session.get(JobSource, job.source_id)
        category = (job.category or "").casefold()
        auto_categories = {item.casefold() for item in preferences.auto_send_categories}
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
        start_of_day = datetime.combine(datetime.now(UTC).date(), time.min, UTC)
        attempts_today = await session.scalar(
            select(func.count(EmailDelivery.id))
            .join(Application, Application.id == EmailDelivery.application_id)
            .where(
                EmailDelivery.created_at >= start_of_day,
                Application.profile_id == application.profile_id,
                EmailDelivery.status.in_(
                    {
                        DeliveryStatus.SENT,
                        DeliveryStatus.SENDING,
                        DeliveryStatus.DELIVERY_UNKNOWN,
                    }
                ),
            )
        )
        sent_today = await session.scalar(
            select(func.count(Application.id)).where(
                Application.profile_id == application.profile_id,
                Application.status == ApplicationStatus.SENT,
                Application.sent_at >= start_of_day,
            )
        )
        raw_minimum_daily = additional_rules.get("minimum_daily_applications", 0)
        try:
            minimum_daily = max(
                0, min(int(raw_minimum_daily), preferences.maximum_daily_applications)
            )
        except (TypeError, ValueError):
            minimum_daily = 0
        force_minimum = additional_rules.get("force_minimum_daily_applications") is True
        minimum_catchup_active = force_minimum and int(sent_today or 0) < minimum_daily
        prior_unknown = await session.scalar(
            select(func.count(EmailDelivery.id)).where(
                EmailDelivery.application_id == application.id,
                EmailDelivery.status == DeliveryStatus.DELIVERY_UNKNOWN,
            )
        )

        rule("deployment_emergency_switch_off", not self.settings.emergency_email_kill_switch)
        rule("auto_send_enabled", preferences.auto_send_enabled)
        rule("global_pause_off", not preferences.global_pause)
        rule("source_healthy", bool(source and source.health_status == SourceHealth.HEALTHY))
        rule(
            "source_actions_enabled",
            bool(source and source.enabled and not source.automatic_actions_paused),
        )
        rule("category_allowed_for_auto_send", category in auto_categories)
        rule("job_title_allowed_by_preferences", not title_forbidden)
        rule(
            "overall_score_threshold",
            minimum_catchup_active or evaluation.overall_fit >= preferences.minimum_auto_send_score,
        )
        rule("mandatory_requirements_met", not evaluation.missing_requirements)
        rule(
            "match_not_blocked",
            evaluation.decision != MatchDecision.BLOCK,
        )
        rule(
            "match_not_skipped",
            minimum_catchup_active or evaluation.decision != MatchDecision.SKIP,
        )
        rule(
            "match_auto_apply",
            minimum_catchup_active or evaluation.decision == MatchDecision.AUTO_APPLY,
        )
        rule("verified_email_contact", contact.contact_type == ContactType.EMAIL)
        rule("contact_verified", contact.verification_status == VerificationStatus.VERIFIED)
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
        rule(
            "daily_limit",
            int(attempts_today or 0) < preferences.maximum_daily_applications,
        )

        hard_block_rules = {
            "deployment_emergency_switch_off",
            "source_healthy",
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
        }
        if hard_block_rules & set(failed):
            decision = PolicyDecision.BLOCKED
        elif evaluation.decision == MatchDecision.SKIP and not minimum_catchup_active:
            decision = PolicyDecision.SKIPPED
        elif failed:
            decision = PolicyDecision.PENDING_REVIEW
        else:
            decision = PolicyDecision.AUTO_APPROVED
        return PolicyResult(
            decision=decision,
            rules_passed=passed,
            rules_failed=failed,
            policy_version=POLICY_VERSION,
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
        application.policy_result = result.model_dump(mode="json")
        status_map = {
            PolicyDecision.AUTO_APPROVED: ApplicationStatus.AUTO_APPROVED,
            PolicyDecision.PENDING_REVIEW: ApplicationStatus.PENDING_REVIEW,
            PolicyDecision.BLOCKED: ApplicationStatus.BLOCKED,
            PolicyDecision.SKIPPED: ApplicationStatus.CANCELLED,
        }
        application.status = status_map[result.decision]
        await session.flush()
        return result
