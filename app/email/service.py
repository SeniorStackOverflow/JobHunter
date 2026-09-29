from __future__ import annotations

import hashlib
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import structlog
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit import record_audit_event
from app.contacts import contact_is_source_verified, validate_public_email
from app.email.oauth import GmailOAuthService
from app.email.providers import (
    GMAIL_READONLY_SCOPE,
    GMAIL_REAUTH_REQUIRED_CODE,
    GMAIL_SEND_SCOPE,
    DeliveryUnknownError,
    EmailProvider,
    FakeGmailProvider,
    GmailApiProvider,
    GmailReauthorizationRequired,
    PermanentDeliveryError,
    PreparedEmail,
    TemporaryDeliveryError,
    deterministic_message_id,
)
from app.email.retries import retry_delay
from app.employers import EmployerIdentityService, EmployerRelationshipService
from app.matching.bindings import (
    evaluation_inputs_are_current,
    used_confirmed_facts_are_current,
)
from app.matching.freshness import evaluation_is_current
from app.models.constants import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.models.entities import (
    Account,
    Application,
    EmailDelivery,
    EmployerContact,
    JobPreference,
    MatchEvaluation,
    Resume,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    AccountStatus,
    ApplicationStatus,
    ContactDeliveryState,
    ContactType,
    DeliveryStatus,
    EmployerInteractionChannel,
    EmployerInteractionType,
    JobStatus,
    PolicyDecision,
    ProfileStatus,
)
from app.observability.metrics import EMAIL_DELIVERIES, SEND_BLOCKED_RELATIONSHIP_CHANGED
from app.policies import PolicyEngine
from app.policies.schemas import PolicyResult
from app.profiles.service import choose_resume_for_job
from app.security.files import UnsafeResumeError, read_verified_resume
from app.settings import Settings, get_settings
from app.time_utils import local_day_bounds


class EmailSendBlocked(ValueError):
    """Persisted state does not authorize this delivery."""

    def __init__(self, message: str, *, reason: str = "delivery_not_authorized") -> None:
        super().__init__(message)
        self.reason = reason


logger = structlog.get_logger(__name__)

_AUTO_SEND_HARD_FAILURES = {
    "source_actions_enabled",
    "source_selected_for_profile",
    "match_not_blocked",
    "match_not_skipped",
    "deterministic_hard_requirements_met",
    "hard_requirement_binding_current",
    "vacancy_active",
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
_AUTO_SEND_TRANSIENT_FAILURES = {
    "deployment_emergency_switch_off",
    "auto_send_enabled",
    "global_pause_off",
    "daily_limit",
    "source_healthy",
    "no_active_employer_conversation",
    "employer_application_slot_available",
}


class EmailService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        provider: EmailProvider | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._provider = provider
        self.employer_relationships = EmployerRelationshipService()

    @staticmethod
    def _apply_safe_stop(
        application: Application,
        *,
        status: ApplicationStatus,
        reason: str,
        failed_rules: tuple[str, ...] = (),
        policy: PolicyResult | None = None,
        requires_rematch: bool = False,
    ) -> bool:
        if policy is not None:
            policy_result = policy.model_dump(mode="json")
        else:
            policy_result = dict(application.policy_result)
        decision = {
            ApplicationStatus.PENDING_REVIEW: PolicyDecision.PENDING_REVIEW,
            ApplicationStatus.DEFERRED: PolicyDecision.DEFERRED,
            ApplicationStatus.BLOCKED: PolicyDecision.BLOCKED,
        }[status]
        passed = [
            item
            for item in policy_result.get("rules_passed", [])
            if isinstance(item, str) and item not in failed_rules
        ]
        failed = [item for item in policy_result.get("rules_failed", []) if isinstance(item, str)]
        for rule_name in failed_rules:
            if rule_name not in failed:
                failed.append(rule_name)
        policy_result.update(
            {
                "decision": decision.value,
                "rules_passed": passed,
                "rules_failed": failed,
                "safe_stop_reason": reason,
                "requires_rematch": requires_rematch,
            }
        )
        changed = (
            application.status != status
            or application.policy_decision != decision
            or application.policy_result != policy_result
        )
        if not changed:
            return False
        application.status = status
        application.policy_decision = decision
        application.policy_result = policy_result
        return True

    @staticmethod
    async def _persist_safe_stop(
        session: AsyncSession,
        application: Application,
        *,
        status: ApplicationStatus,
        reason: str,
        failed_rules: tuple[str, ...] = (),
        policy: PolicyResult | None = None,
        requires_rematch: bool = False,
    ) -> None:
        EmailService._apply_safe_stop(
            application,
            status=status,
            reason=reason,
            failed_rules=failed_rules,
            policy=policy,
            requires_rematch=requires_rematch,
        )
        await record_audit_event(
            session,
            actor="email_worker",
            action="email.delivery_blocked",
            entity_type="application",
            entity_id=str(application.id),
            correlation_id=str(application.id),
            decision=status.value,
            details={
                "reason": reason,
                "failed_rules": list(failed_rules),
                "requires_rematch": requires_rematch,
            },
        )
        await session.commit()

    async def reconcile_auto_approved_applications(
        self,
        session: AsyncSession,
        *,
        application_ids: Collection[UUID] | None = None,
    ) -> dict[str, int]:
        """Reconcile unsafe current and legacy auto-approval states before delivery."""

        query = (
            select(Application, SourceJob, MatchEvaluation)
            .outerjoin(
                SourceJob,
                and_(
                    SourceJob.id == Application.source_job_id,
                    SourceJob.canonical_job_id == Application.canonical_job_id,
                ),
            )
            .outerjoin(
                MatchEvaluation,
                and_(
                    MatchEvaluation.id == Application.match_evaluation_id,
                    MatchEvaluation.profile_id == Application.profile_id,
                    MatchEvaluation.source_job_id == Application.source_job_id,
                    MatchEvaluation.canonical_job_id == Application.canonical_job_id,
                ),
            )
            .where(
                or_(
                    Application.status == ApplicationStatus.AUTO_APPROVED,
                    and_(
                        Application.status == ApplicationStatus.PENDING_REVIEW,
                        Application.policy_decision == PolicyDecision.AUTO_APPROVED,
                    ),
                )
            )
        )
        if application_ids is not None:
            if not application_ids:
                return {}
            query = query.where(Application.id.in_(application_ids))
        query = query.order_by(Application.id).with_for_update(
            of=Application,
            skip_locked=True,
        )
        rows = (await session.execute(query)).all()
        if not rows:
            return {}
        profile_ids = {application.profile_id for application, _job, _evaluation in rows}
        contact_ids = {application.recipient_contact_id for application, _job, _evaluation in rows}
        profiles = {
            profile.id: profile
            for profile in (
                await session.scalars(select(UserProfile).where(UserProfile.id.in_(profile_ids)))
            ).all()
        }
        preferences_by_profile = {
            preference.profile_id: preference
            for preference in (
                await session.scalars(
                    select(JobPreference).where(JobPreference.profile_id.in_(profile_ids))
                )
            ).all()
        }
        contacts = {
            contact.id: contact
            for contact in (
                await session.scalars(
                    select(EmployerContact).where(EmployerContact.id.in_(contact_ids))
                )
            ).all()
        }
        resumes_by_profile: dict[UUID, list[Resume]] = {}
        for resume in (
            await session.scalars(
                select(Resume).where(
                    Resume.profile_id.in_(profile_ids),
                    Resume.active.is_(True),
                    Resume.verified.is_(True),
                )
            )
        ).all():
            resumes_by_profile.setdefault(resume.profile_id, []).append(resume)
        counts: dict[str, int] = {}
        policy_engine = PolicyEngine(self.settings)
        for application, job, evaluation in rows:
            status: ApplicationStatus | None = None
            reason: str | None = None
            failed_rules: tuple[str, ...] = ()
            policy: PolicyResult | None = None
            requires_rematch = False

            if job is None or evaluation is None:
                status = ApplicationStatus.BLOCKED
                reason = "invalid_match_evaluation_binding"
                failed_rules = ("match_evaluation_binding_valid",)
            elif job.status != JobStatus.ACTIVE:
                status = ApplicationStatus.BLOCKED
                reason = "vacancy_not_active"
                failed_rules = ("vacancy_active",)
            elif not await evaluation_is_current(session, evaluation, job):
                status = ApplicationStatus.PENDING_REVIEW
                reason = "match_evaluation_stale"
                failed_rules = ("match_evaluation_current",)
                requires_rematch = True
            else:
                profile = profiles.get(application.profile_id)
                preferences = preferences_by_profile.get(application.profile_id)
                contact = contacts.get(application.recipient_contact_id)
                current_resumes = resumes_by_profile.get(application.profile_id, [])
                selected_resume = (
                    choose_resume_for_job(current_resumes, job)
                    if profile is not None and preferences is not None
                    else None
                )
                if profile is None or preferences is None or contact is None:
                    status = ApplicationStatus.BLOCKED
                    reason = "application_dependencies_incomplete"
                    failed_rules = ("application_dependencies_complete",)
                elif (
                    selected_resume is None
                    or selected_resume.id != application.resume_id
                    or not evaluation_inputs_are_current(
                        evaluation,
                        profile,
                        preferences,
                        selected_resume,
                    )
                    or not used_confirmed_facts_are_current(
                        evaluation,
                        profile,
                        application.used_confirmed_facts,
                    )
                ):
                    status = ApplicationStatus.PENDING_REVIEW
                    reason = "match_evaluation_inputs_stale"
                    failed_rules = ("match_evaluation_inputs_current",)
                    requires_rematch = True
                else:
                    current_public_emails = {
                        normalized
                        for value in [job.public_email, *(job.public_emails or [])]
                        if value and (normalized := validate_public_email(value))
                    }
                    if (
                        contact.source_job_id != application.source_job_id
                        or contact.canonical_job_id != application.canonical_job_id
                        or contact.contact_type != ContactType.EMAIL
                        or not contact_is_source_verified(contact)
                        or contact.value not in current_public_emails
                    ):
                        status = ApplicationStatus.BLOCKED
                        reason = "recipient_not_verified"
                        failed_rules = ("contact_verified",)
                    elif not application.content_validated:
                        status = ApplicationStatus.BLOCKED
                        reason = "application_content_not_validated"
                        failed_rules = ("letter_validated",)
                    else:
                        policy = await policy_engine.evaluate(
                            session,
                            application,
                            preferences,
                            evaluation,
                            job,
                            selected_resume,
                            contact,
                            profile,
                        )
                        current_failed = set(policy.rules_failed)
                        if _AUTO_SEND_HARD_FAILURES & current_failed:
                            status = ApplicationStatus.BLOCKED
                            reason = "current_policy_hard_failure"
                            failed_rules = tuple(policy.rules_failed)
                        elif {
                            "no_active_employer_conversation",
                            "employer_application_slot_available",
                        } & current_failed:
                            status = ApplicationStatus.DEFERRED
                            reason = (
                                "active_employer_conversation"
                                if "no_active_employer_conversation" in current_failed
                                else "same_employer_application_deferred"
                            )
                            failed_rules = tuple(policy.rules_failed)
                        elif current_failed - _AUTO_SEND_TRANSIENT_FAILURES:
                            status = ApplicationStatus.PENDING_REVIEW
                            reason = "current_policy_requires_review"
                            failed_rules = tuple(policy.rules_failed)

            if status is None or reason is None:
                continue
            changed = self._apply_safe_stop(
                application,
                status=status,
                reason=reason,
                failed_rules=failed_rules,
                policy=policy,
                requires_rematch=requires_rematch,
            )
            if not changed:
                continue
            await record_audit_event(
                session,
                actor="email_scheduler",
                action="application.delivery_preflight_reconciled",
                entity_type="application",
                entity_id=str(application.id),
                correlation_id=str(application.id),
                decision=status.value,
                details={
                    "reason": reason,
                    "failed_rules": list(failed_rules),
                    "requires_rematch": requires_rematch,
                },
            )
            counts[reason] = counts.get(reason, 0) + 1
        await session.flush()
        return counts

    async def _provider_for(
        self,
        session: AsyncSession,
        *,
        account_id: UUID = BOOTSTRAP_ADMIN_ACCOUNT_ID,
    ) -> EmailProvider:
        if self._provider is not None:
            if self.settings.environment != "test" and isinstance(
                self._provider,
                FakeGmailProvider,
            ):
                raise EmailSendBlocked(
                    "fake email providers are restricted to the test environment"
                )
            # Injected providers and the fake test provider are intentionally reusable.
            # A real Gmail provider is different: it contains decrypted OAuth material
            # and may retain an access token. Reusing it after a local disconnect would
            # let the remainder of a worker batch keep sending with stale authority.
            if not isinstance(self._provider, GmailApiProvider):
                return self._provider
        if self.settings.email_provider == "fake":
            if self.settings.environment != "test":
                raise EmailSendBlocked("fake email delivery is restricted to the test environment")
            self._provider = FakeGmailProvider()
            return self._provider
        if not self.settings.real_email_delivery_enabled:
            raise EmailSendBlocked("real email delivery is disabled at deployment level")
        oauth = GmailOAuthService(self.settings)
        refresh_token = await oauth.get_refresh_token(
            session,
            account_id=account_id,
            required_scopes=(GMAIL_SEND_SCOPE, GMAIL_READONLY_SCOPE),
        )
        if self.settings.gmail_client_id is None or self.settings.gmail_client_secret is None:
            raise EmailSendBlocked("Gmail OAuth client is incomplete")
        # Do not cache a real provider. Every logical send must re-read the current
        # OAuthCredential, so deleting it is an effective gate for the next message.
        return GmailApiProvider(
            client_id=self.settings.gmail_client_id.get_secret_value(),
            client_secret=self.settings.gmail_client_secret.get_secret_value(),
            refresh_token=refresh_token,
        )

    async def send_application(self, application_id: UUID) -> EmailDelivery:
        async with self.session_factory() as session:
            application = await session.scalar(
                select(Application).where(Application.id == application_id).with_for_update()
            )
            if application is None:
                raise LookupError(f"application {application_id} does not exist")
            active_profile = await session.scalar(
                select(UserProfile)
                .outerjoin(Account, Account.id == UserProfile.owner_account_id)
                .where(
                    UserProfile.id == application.profile_id,
                    UserProfile.status == ProfileStatus.ACTIVE,
                    or_(
                        UserProfile.owner_account_id == BOOTSTRAP_ADMIN_ACCOUNT_ID,
                        Account.status == AccountStatus.ACTIVE,
                    ),
                )
            )
            if active_profile is None:
                await self._persist_safe_stop(
                    session,
                    application,
                    status=ApplicationStatus.DEFERRED,
                    reason="account_or_profile_inactive",
                    failed_rules=("account_profile_active",),
                )
                raise EmailSendBlocked(
                    "account or profile is not active",
                    reason="account_or_profile_inactive",
                )
            existing = await session.scalar(
                select(EmailDelivery).where(EmailDelivery.application_id == application_id)
            )
            fallback_pending = bool(
                existing is not None
                and dict(existing.sanitized_provider_response).get("recipient_fallback_pending")
            )
            if (
                existing is not None
                and existing.status
                in {
                    DeliveryStatus.SENT,
                    DeliveryStatus.PROVIDER_ACCEPTED,
                    DeliveryStatus.DELIVERED,
                    DeliveryStatus.DELIVERY_UNKNOWN,
                    DeliveryStatus.SENDING,
                    DeliveryStatus.BOUNCED_PERMANENT,
                    DeliveryStatus.RECIPIENT_REJECTED,
                    DeliveryStatus.DOMAIN_REJECTED,
                    DeliveryStatus.POLICY_REJECTED,
                    DeliveryStatus.SPAM_REJECTED,
                    DeliveryStatus.DELIVERY_FAILED,
                }
                and not (fallback_pending and existing.status is DeliveryStatus.RECIPIENT_REJECTED)
            ):
                return existing
            if application.status not in {
                ApplicationStatus.AUTO_APPROVED,
                ApplicationStatus.APPROVED,
                ApplicationStatus.FAILED,
            }:
                raise EmailSendBlocked("application is not approved for delivery")
            retryable_failure = existing is not None and (
                existing.status
                in {
                    DeliveryStatus.TEMPORARY_FAILURE,
                    DeliveryStatus.BOUNCED_TRANSIENT,
                    DeliveryStatus.MAILBOX_FULL,
                }
                or (fallback_pending and existing.status is DeliveryStatus.RECIPIENT_REJECTED)
            )
            if application.status == ApplicationStatus.FAILED and (
                existing is None
                or not retryable_failure
                or existing.error_code == GMAIL_REAUTH_REQUIRED_CODE
                or existing.attempt_count >= self.settings.email_delivery_max_attempts
                or (
                    existing.next_retry_at is not None
                    and (
                        existing.next_retry_at.replace(tzinfo=UTC)
                        if existing.next_retry_at.tzinfo is None
                        else existing.next_retry_at.astimezone(UTC)
                    )
                    > datetime.now(UTC)
                )
            ):
                raise EmailSendBlocked("failed application is not safely retryable")

            # PostgreSQL serializes the policy check and provider-attempt reservation for
            # all applications in the same Europe/Chisinau calendar day. The lock is
            # transaction-scoped and released before the provider call.
            bind = session.get_bind()
            if bind.dialect.name == "postgresql":
                start_local, _start_of_day, _end_of_day = local_day_bounds()
                quota_lock = f"job-agent:email-daily:{start_local.date().isoformat()}"
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:quota_lock))"),
                    {"quota_lock": quota_lock},
                )

            job = await session.scalar(
                select(SourceJob)
                .where(
                    SourceJob.id == application.source_job_id,
                    SourceJob.canonical_job_id == application.canonical_job_id,
                )
                .with_for_update()
            )
            if job is not None and application.employer_id is None:
                identity = await EmployerIdentityService().resolve_for_source_job(session, job)
                application.employer_id = identity.employer.id
            if application.employer_id is None:
                await self._persist_safe_stop(
                    session,
                    application,
                    status=ApplicationStatus.PENDING_REVIEW,
                    reason="employer_identity_unresolved",
                    failed_rules=("employer_identity_resolved",),
                )
                raise EmailSendBlocked(
                    "application has no canonical employer",
                    reason="employer_identity_unresolved",
                )
            await self.employer_relationships.lock_employer(session, application.employer_id)

            resume = await session.get(Resume, application.resume_id)
            contact = await session.get(EmployerContact, application.recipient_contact_id)
            preferences = await session.scalar(
                select(JobPreference).where(JobPreference.profile_id == application.profile_id)
            )
            profile = await session.get(UserProfile, application.profile_id)
            evaluation = await session.scalar(
                select(MatchEvaluation).where(
                    MatchEvaluation.id == application.match_evaluation_id,
                    MatchEvaluation.profile_id == application.profile_id,
                    MatchEvaluation.source_job_id == application.source_job_id,
                    MatchEvaluation.canonical_job_id == application.canonical_job_id,
                )
            )
            if job is None or evaluation is None:
                await self._persist_safe_stop(
                    session,
                    application,
                    status=ApplicationStatus.BLOCKED,
                    reason="invalid_match_evaluation_binding",
                    failed_rules=("match_evaluation_binding_valid",),
                )
                raise EmailSendBlocked(
                    "application is not bound to an evaluation for the same source publication",
                    reason="invalid_match_evaluation_binding",
                )
            if any(value is None for value in (resume, contact, preferences, profile)):
                await self._persist_safe_stop(
                    session,
                    application,
                    status=ApplicationStatus.BLOCKED,
                    reason="application_dependencies_incomplete",
                    failed_rules=("application_dependencies_complete",),
                )
                raise EmailSendBlocked(
                    "application dependencies are incomplete",
                    reason="application_dependencies_incomplete",
                )
            if resume is not None and resume.profile_id != application.profile_id:
                raise EmailSendBlocked("resume belongs to another profile")
            if evaluation is not None and evaluation.profile_id != application.profile_id:
                raise EmailSendBlocked("evaluation belongs to another profile")
            if preferences is not None and preferences.profile_id != application.profile_id:
                raise EmailSendBlocked("preferences belong to another profile")
            assert resume is not None
            assert contact is not None
            assert preferences is not None
            assert profile is not None
            if not await evaluation_is_current(session, evaluation, job):
                await self._persist_safe_stop(
                    session,
                    application,
                    status=ApplicationStatus.PENDING_REVIEW,
                    reason="match_evaluation_stale",
                    failed_rules=("match_evaluation_current",),
                    requires_rematch=True,
                )
                raise EmailSendBlocked(
                    "the bound match evaluation is stale for this source publication",
                    reason="match_evaluation_stale",
                )
            current_resumes = list(
                (
                    await session.scalars(
                        select(Resume).where(
                            Resume.profile_id == application.profile_id,
                            Resume.active.is_(True),
                            Resume.verified.is_(True),
                        )
                    )
                ).all()
            )
            currently_selected_resume = choose_resume_for_job(current_resumes, job)
            if (
                currently_selected_resume is None
                or currently_selected_resume.id != application.resume_id
                or not evaluation_inputs_are_current(
                    evaluation,
                    profile,
                    preferences,
                    currently_selected_resume,
                )
                or not used_confirmed_facts_are_current(
                    evaluation,
                    profile,
                    application.used_confirmed_facts,
                )
            ):
                await self._persist_safe_stop(
                    session,
                    application,
                    status=ApplicationStatus.PENDING_REVIEW,
                    reason="match_evaluation_inputs_stale",
                    failed_rules=("match_evaluation_inputs_current",),
                    requires_rematch=True,
                )
                raise EmailSendBlocked(
                    "profile, preferences, resume, or confirmed facts changed after matching",
                    reason="match_evaluation_inputs_stale",
                )
            current_public_emails = {
                normalized
                for value in [job.public_email, *(job.public_emails or [])]
                if value and (normalized := validate_public_email(value))
            }
            if (
                contact.source_job_id != application.source_job_id
                or contact.canonical_job_id != application.canonical_job_id
                or contact.contact_type != ContactType.EMAIL
                or not contact_is_source_verified(contact)
                or contact.value not in current_public_emails
            ):
                await self._persist_safe_stop(
                    session,
                    application,
                    status=ApplicationStatus.BLOCKED,
                    reason="recipient_not_verified",
                    failed_rules=("contact_verified",),
                )
                raise EmailSendBlocked(
                    "recipient is not a verified public email",
                    reason="recipient_not_verified",
                )
            if not resume.active or not resume.verified:
                await self._persist_safe_stop(
                    session,
                    application,
                    status=ApplicationStatus.BLOCKED,
                    reason="resume_not_active_verified",
                    failed_rules=("resume_active_verified",),
                )
                raise EmailSendBlocked(
                    "resume is not active and verified",
                    reason="resume_not_active_verified",
                )
            if not application.content_validated:
                await self._persist_safe_stop(
                    session,
                    application,
                    status=ApplicationStatus.BLOCKED,
                    reason="application_content_not_validated",
                    failed_rules=("letter_validated",),
                )
                raise EmailSendBlocked(
                    "application content is not validated",
                    reason="application_content_not_validated",
                )
            policy = await PolicyEngine(self.settings).evaluate(
                session, application, preferences, evaluation, job, resume, contact, profile
            )
            failed_rules = set(policy.rules_failed)
            auto_send_authority = application.status is ApplicationStatus.AUTO_APPROVED or (
                application.status is ApplicationStatus.FAILED
                and application.policy_decision is PolicyDecision.AUTO_APPROVED
            )
            manual_send_authority = application.status is ApplicationStatus.APPROVED or (
                application.status is ApplicationStatus.FAILED
                and application.policy_decision is not PolicyDecision.AUTO_APPROVED
            )
            if auto_send_authority and policy.decision != PolicyDecision.AUTO_APPROVED:
                safe_stop_reason: str | None = None
                if _AUTO_SEND_HARD_FAILURES & failed_rules:
                    safe_stop_reason = "current_policy_hard_failure"
                    await self._persist_safe_stop(
                        session,
                        application,
                        status=ApplicationStatus.BLOCKED,
                        reason=safe_stop_reason,
                        failed_rules=tuple(policy.rules_failed),
                        policy=policy,
                    )
                    if {"employer_not_suppressed", "no_candidate_withdrawal"} & failed_rules:
                        SEND_BLOCKED_RELATIONSHIP_CHANGED.labels(reason="employer_suppressed").inc()
                elif failed_rules - _AUTO_SEND_TRANSIENT_FAILURES:
                    safe_stop_reason = "current_policy_requires_review"
                    await self._persist_safe_stop(
                        session,
                        application,
                        status=ApplicationStatus.PENDING_REVIEW,
                        reason=safe_stop_reason,
                        failed_rules=tuple(policy.rules_failed),
                        policy=policy,
                    )
                elif {
                    "no_active_employer_conversation",
                    "employer_application_slot_available",
                } & failed_rules:
                    safe_stop_reason = (
                        "active_employer_conversation"
                        if "no_active_employer_conversation" in failed_rules
                        else "same_employer_application_deferred"
                    )
                    await self._persist_safe_stop(
                        session,
                        application,
                        status=ApplicationStatus.DEFERRED,
                        reason=safe_stop_reason,
                        failed_rules=tuple(policy.rules_failed),
                        policy=policy,
                    )
                    SEND_BLOCKED_RELATIONSHIP_CHANGED.labels(reason=safe_stop_reason).inc()
                raise EmailSendBlocked(
                    "current policy no longer permits automatic delivery",
                    reason=safe_stop_reason or "current_policy_transient_failure",
                )
            if manual_send_authority:
                manual_required = _AUTO_SEND_HARD_FAILURES | {
                    "deployment_emergency_switch_off",
                    "global_pause_off",
                    "daily_limit",
                    "source_healthy",
                    "no_active_employer_conversation",
                    "employer_application_slot_available",
                }
                if manual_required & failed_rules:
                    if _AUTO_SEND_HARD_FAILURES & failed_rules:
                        await self._persist_safe_stop(
                            session,
                            application,
                            status=ApplicationStatus.BLOCKED,
                            reason="manual_approval_hard_failure",
                            failed_rules=tuple(policy.rules_failed),
                            policy=policy,
                        )
                    elif {
                        "no_active_employer_conversation",
                        "employer_application_slot_available",
                    } & failed_rules:
                        reason = (
                            "active_employer_conversation"
                            if "no_active_employer_conversation" in failed_rules
                            else "same_employer_application_deferred"
                        )
                        await self._persist_safe_stop(
                            session,
                            application,
                            status=ApplicationStatus.DEFERRED,
                            reason=reason,
                            failed_rules=tuple(policy.rules_failed),
                            policy=policy,
                        )
                        SEND_BLOCKED_RELATIONSHIP_CHANGED.labels(reason=reason).inc()
                    raise EmailSendBlocked(
                        "manual approval cannot override delivery safety rules",
                        reason="manual_approval_policy_failure",
                    )

            try:
                attachment_data = read_verified_resume(
                    self.settings.resume_storage_path,
                    resume.storage_key,
                    expected_sha256=resume.sha256,
                    expected_mime_type=resume.mime_type,
                    max_bytes=self.settings.max_resume_bytes,
                )
            except UnsafeResumeError as exc:
                await self._persist_safe_stop(
                    session,
                    application,
                    status=ApplicationStatus.BLOCKED,
                    reason="resume_integrity_failure",
                    failed_rules=("resume_integrity_valid",),
                )
                raise EmailSendBlocked(
                    "verified resume failed the final integrity check",
                    reason="resume_integrity_failure",
                ) from exc
            message_id_key = (
                f"{application.id}:recipient-fallback:{contact.id}"
                if fallback_pending
                else str(application.id)
            )
            message = PreparedEmail(
                application_id=str(application.id),
                recipient=contact.value,
                subject=application.subject,
                body=application.body,
                attachment_name=Path(resume.original_filename).name,
                attachment_mime_type=resume.mime_type,
                attachment_data=attachment_data,
                message_id=deterministic_message_id(message_id_key),
            )
            authorized_status = application.status
            provider = await self._provider_for(session, account_id=active_profile.owner_account_id)
            if existing is None:
                delivery = EmailDelivery(
                    application_id=application.id,
                    provider=provider.name,
                    recipient=contact.value,
                    status=DeliveryStatus.SENDING,
                    sanitized_provider_response={},
                    attempt_count=1,
                    rfc_message_id=message.message_id,
                    subject_fingerprint=hashlib.sha256(
                        message.subject.encode("utf-8", errors="replace")
                    ).hexdigest(),
                )
                session.add(delivery)
            else:
                delivery = existing
                if fallback_pending:
                    fallback_metadata = dict(delivery.sanitized_provider_response)
                    delivery.recipient = contact.value
                    delivery.final_recipient = None
                    delivery.provider_message_id = None
                    delivery.thread_id = None
                    delivery.bounced_at = None
                    delivery.smtp_status = None
                    delivery.failure_class = None
                    delivery.failure_reason = None
                    delivery.next_retry_at = None
                    delivery.sanitized_provider_response = {
                        **fallback_metadata,
                        "recipient_fallback_pending": False,
                        "recipient_fallback_used": True,
                    }
                delivery.status = DeliveryStatus.SENDING
                delivery.attempt_count += 1
                delivery.error = None
                delivery.error_code = None
                delivery.rfc_message_id = message.message_id
                delivery.subject_fingerprint = hashlib.sha256(
                    message.subject.encode("utf-8", errors="replace")
                ).hexdigest()
            from app.database.base import utcnow

            delivery.last_attempt_at = utcnow()
            contact.last_delivery_attempt_at = delivery.last_attempt_at
            application.status = ApplicationStatus.SENDING
            await session.commit()

            try:
                result = await provider.send(message)
            except GmailReauthorizationRequired as exc:
                delivery.status = DeliveryStatus.TEMPORARY_FAILURE
                delivery.error = str(exc)
                delivery.error_code = GMAIL_REAUTH_REQUIRED_CODE
                delivery.sanitized_provider_response = {
                    **dict(delivery.sanitized_provider_response),
                    "reauth_original_application_status": authorized_status.value,
                }
                application.status = ApplicationStatus.FAILED
                await GmailOAuthService(self.settings).mark_reauthorization_required(
                    session,
                    account_id=active_profile.owner_account_id,
                    error=GMAIL_REAUTH_REQUIRED_CODE,
                )
            except DeliveryUnknownError as exc:
                delivery.status = DeliveryStatus.DELIVERY_UNKNOWN
                delivery.error = str(exc)
                delivery.error_code = None
                application.status = ApplicationStatus.DELIVERY_UNKNOWN
            except TemporaryDeliveryError as exc:
                delivery.status = DeliveryStatus.TEMPORARY_FAILURE
                delivery.error = str(exc)
                delivery.error_code = None
                delivery.next_retry_at = (
                    utcnow() + retry_delay(delivery.attempt_count)
                    if delivery.attempt_count < self.settings.email_delivery_max_attempts
                    else None
                )
                application.status = ApplicationStatus.FAILED
                contact.delivery_state = ContactDeliveryState.TRANSIENT_FAILURE
                contact.last_delivery_failure_at = utcnow()
                contact.failure_count += 1
            except PermanentDeliveryError as exc:
                delivery.status = DeliveryStatus.PERMANENT_FAILURE
                delivery.error = str(exc)
                delivery.error_code = None
                application.status = ApplicationStatus.FAILED
                contact.delivery_state = ContactDeliveryState.REJECTED
                contact.last_delivery_failure_at = utcnow()
                contact.failure_count += 1
            else:
                delivery.status = DeliveryStatus.PROVIDER_ACCEPTED
                delivery.provider_message_id = result.message_id
                delivery.thread_id = result.thread_id
                delivery.sanitized_provider_response = {
                    **dict(delivery.sanitized_provider_response),
                    **result.sanitized_response,
                }
                delivery.error = None
                delivery.error_code = None
                application.status = ApplicationStatus.SENT
                accepted_at = utcnow()
                if application.sent_at is None:
                    application.sent_at = accepted_at
                delivery.submitted_at = accepted_at
                delivery.provider_accepted_at = accepted_at
                delivery.final_recipient = contact.value
                await self.employer_relationships.record_event(
                    session,
                    profile_id=application.profile_id,
                    employer_id=application.employer_id,
                    event_type=EmployerInteractionType.APPLICATION_SENT,
                    channel=EmployerInteractionChannel.EMAIL,
                    idempotency_key=f"application-sent:{application.id}",
                    occurred_at=application.sent_at,
                    application_id=application.id,
                    canonical_job_id=application.canonical_job_id,
                    source_job_id=application.source_job_id,
                    event_metadata={"delivery_id": str(delivery.id)},
                    role=job.title,
                )
                if provider.name == "gmail":
                    await GmailOAuthService(self.settings).mark_refresh_ok(
                        session, account_id=active_profile.owner_account_id
                    )
            await record_audit_event(
                session,
                actor="email_worker",
                action="email.delivery",
                entity_type="application",
                entity_id=str(application.id),
                correlation_id=str(application.id),
                decision=delivery.status.value,
                details={
                    "provider": provider.name,
                    "recipient_domain": contact.value.rsplit("@", maxsplit=1)[-1],
                    "attempt": delivery.attempt_count,
                },
            )
            EMAIL_DELIVERIES.labels(
                provider=provider.name,
                state=delivery.status.value,
            ).inc()
            await session.commit()
            return delivery


async def reconcile_auto_approved_application_states(
    *,
    batch_size: int = 50,
) -> dict[str, int]:
    """Reconcile auto-send states in short deterministic transactions."""
    from app.database.session import async_session_factory

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    service = EmailService(get_settings(), async_session_factory)
    totals: dict[str, int] = {}
    last_id: UUID | None = None
    scanned = 0
    batches = 0

    while True:
        async with async_session_factory() as session:
            candidate_query = select(Application.id).where(
                or_(
                    Application.status == ApplicationStatus.AUTO_APPROVED,
                    and_(
                        Application.status == ApplicationStatus.PENDING_REVIEW,
                        Application.policy_decision == PolicyDecision.AUTO_APPROVED,
                    ),
                )
            )
            if last_id is not None:
                candidate_query = candidate_query.where(Application.id > last_id)
            application_ids = list(
                (
                    await session.scalars(
                        candidate_query.order_by(Application.id).limit(batch_size)
                    )
                ).all()
            )
            if not application_ids:
                break

            counts = await service.reconcile_auto_approved_applications(
                session,
                application_ids=application_ids,
            )
            await session.commit()

        batches += 1
        scanned += len(application_ids)
        last_id = application_ids[-1]
        for reason, count in counts.items():
            totals[reason] = totals.get(reason, 0) + count
        if len(application_ids) < batch_size:
            break

    logger.info(
        "automatic_email_reconciliation_completed",
        scanned=scanned,
        changed=sum(totals.values()),
        batches=batches,
        reasons=totals,
    )
    return totals


async def send_auto_approved_applications() -> int:
    from app.database.session import async_session_factory

    settings = get_settings()
    if settings.environment != "test" and not settings.real_email_delivery_enabled:
        return 0

    service = EmailService(settings, async_session_factory)
    _start_local, start_of_day, _end_of_day = local_day_bounds()
    async with async_session_factory() as session:
        attempt_rows = (
            await session.execute(
                select(Application.profile_id, func.count(EmailDelivery.id))
                .join(EmailDelivery, EmailDelivery.application_id == Application.id)
                .where(
                    EmailDelivery.created_at >= start_of_day,
                    EmailDelivery.status.in_(
                        {
                            DeliveryStatus.SENT,
                            DeliveryStatus.PROVIDER_ACCEPTED,
                            DeliveryStatus.DELIVERED,
                            DeliveryStatus.SENDING,
                            DeliveryStatus.DELIVERY_UNKNOWN,
                        }
                    ),
                )
                .group_by(Application.profile_id)
            )
        ).all()
        attempts_by_profile: dict[UUID, int] = {
            profile_id: int(attempt_count) for profile_id, attempt_count in attempt_rows
        }
        preference_rows = (
            await session.execute(
                select(
                    JobPreference.profile_id,
                    JobPreference.maximum_daily_applications,
                    JobPreference.auto_send_enabled,
                    JobPreference.global_pause,
                )
                .join(UserProfile, UserProfile.id == JobPreference.profile_id)
                .join(Account, Account.id == UserProfile.owner_account_id)
                .where(
                    UserProfile.status == ProfileStatus.ACTIVE,
                    Account.status == AccountStatus.ACTIVE,
                )
            )
        ).all()
        if not preference_rows:
            logger.info("automatic_email_deferred", reason="missing_preferences")
            return 0
        enabled_rows = [row for row in preference_rows if row.auto_send_enabled]
        if not enabled_rows:
            logger.info("automatic_email_deferred", reason="auto_send_disabled")
            return 0
        eligible_rows = [row for row in enabled_rows if not row.global_pause]
        if not eligible_rows:
            logger.info("automatic_email_deferred", reason="global_pause")
            return 0
        capacities = {
            profile_id: max(0, maximum - int(attempts_by_profile.get(profile_id, 0)))
            for profile_id, maximum, _auto_send_enabled, _global_pause in eligible_rows
        }
        if not any(capacities.values()):
            logger.info("automatic_email_deferred", reason="daily_limit")
            return 0
        candidate_rows = (
            await session.execute(
                select(
                    Application.id,
                    Application.profile_id,
                    UserProfile.owner_account_id,
                )
                .join(JobPreference, JobPreference.profile_id == Application.profile_id)
                .join(UserProfile, UserProfile.id == Application.profile_id)
                .join(Account, Account.id == UserProfile.owner_account_id)
                .where(
                    Application.status == ApplicationStatus.AUTO_APPROVED,
                    JobPreference.auto_send_enabled.is_(True),
                    JobPreference.global_pause.is_(False),
                    UserProfile.status == ProfileStatus.ACTIVE,
                    Account.status == AccountStatus.ACTIVE,
                )
                .order_by(Application.created_at, Application.id)
            )
        ).all()
        applications: list[tuple[UUID, UUID]] = []
        for application_id, profile_id, account_id in candidate_rows:
            remaining = capacities.get(profile_id, 0)
            if remaining <= 0:
                continue
            applications.append((application_id, account_id))
            capacities[profile_id] = remaining - 1
    sent = 0
    blocked_accounts: set[UUID] = set()
    for application_id, account_id in applications:
        if account_id in blocked_accounts:
            continue
        try:
            delivery = await service.send_application(application_id)
        except EmailSendBlocked as exc:
            logger.warning(
                "automatic_email_skipped",
                application_id=str(application_id),
                error_type=type(exc).__name__,
                reason=exc.reason,
            )
            continue
        except LookupError as exc:
            logger.warning(
                "automatic_email_skipped",
                application_id=str(application_id),
                error_type=type(exc).__name__,
                reason="application_missing",
            )
            continue
        if delivery.status in {DeliveryStatus.PROVIDER_ACCEPTED, DeliveryStatus.DELIVERED}:
            sent += 1
        if delivery.error_code == GMAIL_REAUTH_REQUIRED_CODE:
            blocked_accounts.add(account_id)
            logger.warning(
                "automatic_email_account_deferred",
                account_id=str(account_id),
                reason=GMAIL_REAUTH_REQUIRED_CODE,
            )
    return sent


async def retry_temporary_failures() -> int:
    from app.database.session import async_session_factory

    settings = get_settings()
    service = EmailService(settings, async_session_factory)
    async with async_session_factory() as session:
        retry_rows = list(
            (
                await session.execute(
                    select(EmailDelivery.application_id, UserProfile.owner_account_id)
                    .join(Application, Application.id == EmailDelivery.application_id)
                    .join(UserProfile, UserProfile.id == Application.profile_id)
                    .join(Account, Account.id == UserProfile.owner_account_id)
                    .where(
                        UserProfile.status == ProfileStatus.ACTIVE,
                        Account.status == AccountStatus.ACTIVE,
                        or_(
                            and_(
                                EmailDelivery.status.in_(
                                    {
                                        DeliveryStatus.TEMPORARY_FAILURE,
                                        DeliveryStatus.BOUNCED_TRANSIENT,
                                        DeliveryStatus.MAILBOX_FULL,
                                    }
                                ),
                                or_(
                                    EmailDelivery.next_retry_at.is_(None),
                                    EmailDelivery.next_retry_at <= datetime.now(UTC),
                                ),
                            ),
                            and_(
                                EmailDelivery.status == DeliveryStatus.RECIPIENT_REJECTED,
                                EmailDelivery.next_retry_at.is_not(None),
                                EmailDelivery.next_retry_at <= datetime.now(UTC),
                            ),
                        ),
                        or_(
                            EmailDelivery.error_code.is_(None),
                            EmailDelivery.error_code != GMAIL_REAUTH_REQUIRED_CODE,
                        ),
                        EmailDelivery.attempt_count < settings.email_delivery_max_attempts,
                        Application.status.in_(
                            {
                                ApplicationStatus.AUTO_APPROVED,
                                ApplicationStatus.APPROVED,
                                ApplicationStatus.FAILED,
                            }
                        ),
                    )
                )
            ).all()
        )
    retried = 0
    blocked_accounts: set[UUID] = set()
    for application_id, account_id in retry_rows:
        if account_id in blocked_accounts:
            continue
        try:
            delivery = await service.send_application(application_id)
        except EmailSendBlocked as exc:
            logger.warning(
                "temporary_email_retry_skipped",
                application_id=str(application_id),
                error_type=type(exc).__name__,
                reason=exc.reason,
            )
            continue
        except LookupError as exc:
            logger.warning(
                "temporary_email_retry_skipped",
                application_id=str(application_id),
                error_type=type(exc).__name__,
                reason="application_missing",
            )
            continue
        if delivery.status in {DeliveryStatus.PROVIDER_ACCEPTED, DeliveryStatus.DELIVERED}:
            retried += 1
        if delivery.error_code == GMAIL_REAUTH_REQUIRED_CODE:
            blocked_accounts.add(account_id)
            logger.warning(
                "temporary_email_retry_account_deferred",
                account_id=str(account_id),
                reason=GMAIL_REAUTH_REQUIRED_CODE,
            )
    return retried
