from __future__ import annotations

import time
from uuid import UUID, uuid4

from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.availability import block_closed_vacancy_applications
from app.applications.daily_target import catchup_stage, daily_target_state, lock_daily_target
from app.applications.states import ensure_transition
from app.contacts import ContactDiscoveryService
from app.crawlers.parsing.normalization import (
    detect_prompt_injection,
    normalize_for_fingerprint,
    stable_hash,
)
from app.employers import EmployerIdentityService, EmployerRelationshipService
from app.matching.bindings import evaluation_inputs_are_current
from app.matching.freshness import evaluation_is_current
from app.models.entities import (
    Application,
    ApplicationPolicyRefreshQueue,
    CanonicalJob,
    EmailDelivery,
    EmployerContact,
    MatchEvaluation,
    Resume,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    ContactType,
    JobStatus,
    MatchDecision,
    PolicyDecision,
)
from app.policies import PolicyEngine
from app.policy_refresh_queue import enqueue_employer_policy_refresh
from app.profiles import ProfileService, ResumeService
from app.settings import Settings, get_settings
from app.time_utils import local_day_bounds


class ApplicationPreparationError(ValueError):
    """A safe application cannot be prepared from the persisted state."""


_POLICY_ONLY_REFRESH_RULES = {
    "deployment_emergency_switch_off",
    "auto_send_enabled",
    "global_pause_off",
    "source_healthy",
    "source_actions_enabled",
    "category_allowed_for_auto_send",
    "overall_score_threshold",
    "match_auto_apply",
    "vacancy_active",
    "verified_email_contact",
    "contact_verified",
    "resume_active_verified",
    "daily_limit",
    "employer_identity_resolved",
    "employer_not_suppressed",
    "no_candidate_withdrawal",
    "no_active_employer_conversation",
    "employer_application_slot_available",
    "distinct_employer_today",
    "match_not_skipped",
}


def _policy_only_refresh_needed(application: Application) -> bool:
    if (application.policy_result or {}).get("owner_rejected") is True:
        return False
    raw_failed = (application.policy_result or {}).get("rules_failed", [])
    failed = {item for item in raw_failed if isinstance(item, str)}
    return bool(failed) and failed <= _POLICY_ONLY_REFRESH_RULES


def _confirmed_fact(profile: UserProfile, job: SourceJob) -> tuple[str | None, str | None]:
    haystack = f"{job.title} {job.description or ''}".casefold()
    for fact in profile.confirmed_facts:
        if fact.get("confirmed") is not True:
            continue
        statement = str(fact.get("statement") or fact.get("text") or "").strip()
        identifier = str(fact.get("id") or statement)
        keywords = [str(item).casefold() for item in fact.get("keywords", [])]
        if not keywords:
            keywords = [
                word
                for word in statement.casefold().split()
                if len(word) >= 4 and word not in {"have", "with", "experience"}
            ]
        if statement and keywords and any(keyword in haystack for keyword in keywords):
            return identifier, statement
    return None, None


def _has_language(profile: UserProfile, code: str) -> bool:
    return any(
        str(item.get("code", "")).casefold() == code.casefold() and item.get("confirmed") is True
        for item in profile.languages
    )


_SIGNATURE_CLOSERS = {
    "ru": "С уважением,",  # noqa: RUF001 - intentional Cyrillic text
    "ro": "Cu respect,",
    "en": "Kind regards,",
}
_SIGNATURE_CONTACT_LABELS = {
    "ru": ("Тел.: ", "Email: "),
    "ro": ("Tel.: ", "Email: "),
    "en": ("Phone: ", "Email: "),
}


def _letter_signature(profile: UserProfile, language: str) -> str:
    lines = [_SIGNATURE_CLOSERS[language], profile.name]
    phone_label, email_label = _SIGNATURE_CONTACT_LABELS[language]
    phone = (profile.phone or "").strip()
    email = (profile.contact_email or "").strip()
    if phone:
        lines.append(f"{phone_label}{phone}")
    if email:
        lines.append(f"{email_label}{email}")
    return "\n".join(lines)


def generate_letter(profile: UserProfile, job: SourceJob) -> tuple[str, str, str, list[str]]:
    requested = (job.page_locale or "en").split("-", maxsplit=1)[0].casefold()
    language: str | None
    if requested in {"ru", "ro", "en"} and _has_language(profile, requested):
        language = requested
    else:
        language = next(
            (code for code in ("en", "ru", "ro") if _has_language(profile, code)),
            None,
        )
    if language is None:
        raise ApplicationPreparationError("no confirmed language is available for the letter")
    company = job.company or "hiring team"
    fact_id, fact = _confirmed_fact(profile, job)
    if language == "ru":
        subject = f"Отклик на вакансию «{job.title}»"
        relevance = f" Мой релевантный опыт: {fact}" if fact else ""
        body = (
            f"Здравствуйте, команда {company}!\n\n"
            f"Хочу откликнуться на вакансию «{job.title}».{relevance} "
            "Буду рад обсудить требования и формат работы.\n\n"
            f"{_letter_signature(profile, language)}"
        )
    elif language == "ro":
        subject = f"Candidatură pentru postul „{job.title}”"
        relevance = f" Experiența mea relevantă: {fact}" if fact else ""
        body = (
            f"Bună ziua, echipa {company}!\n\n"
            f"Doresc să candidez pentru postul „{job.title}”.{relevance} "
            "Aș aprecia ocazia de a discuta cerințele și programul.\n\n"
            f"{_letter_signature(profile, language)}"
        )
    else:
        subject = f"Application for {job.title}"
        relevance = f" One confirmed relevant fact is: {fact}" if fact else ""
        body = (
            f"Hello {company} team,\n\n"
            f"I would like to apply for the {job.title} position.{relevance} "
            "I would welcome a conversation about the requirements and working arrangement.\n\n"
            f"{_letter_signature(profile, language)}"
        )
    return subject, body, language, [fact_id] if fact_id else []


class ApplicationService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.profile_service = ProfileService()
        self.resume_service = ResumeService(settings)
        self.contact_service = ContactDiscoveryService()
        self.employer_identity = EmployerIdentityService()
        self.policy_engine = PolicyEngine(settings)

    async def prepare(
        self,
        session: AsyncSession,
        canonical_job_id: UUID,
        profile_id: UUID | None = None,
        *,
        require_employer_slot: bool = False,
    ) -> Application:
        profile = await self.profile_service.get_profile(session, profile_id)
        if profile is None:
            raise ApplicationPreparationError("profile is required")
        active_profile = await self.profile_service.get_processing_profile(session, profile.id)
        if active_profile is None:
            raise ApplicationPreparationError("profile or account is not active")
        profile = active_profile
        profile_id = profile.id
        existing = await session.scalar(
            select(Application)
            .where(
                Application.canonical_job_id == canonical_job_id,
                Application.profile_id == profile_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        # Never rewrite content or bindings after a provider attempt. In particular,
        # SENT and DELIVERY_UNKNOWN are immutable idempotency terminal states.
        if existing is not None and existing.status in {
            ApplicationStatus.SENDING,
            ApplicationStatus.SENT,
            ApplicationStatus.DELIVERY_UNKNOWN,
        }:
            return existing
        if existing is not None:
            if (existing.policy_result or {}).get("owner_rejected") is True:
                return existing
            attempted_delivery = await session.scalar(
                select(EmailDelivery.id).where(EmailDelivery.application_id == existing.id)
            )
            if attempted_delivery is not None:
                return existing
        await lock_daily_target(session)
        canonical = await session.get(CanonicalJob, canonical_job_id)
        if canonical is None:
            raise LookupError(f"canonical job {canonical_job_id} does not exist")
        preferences = await self.profile_service.get_preferences(session, profile_id)
        # Consider only the newest evaluation for each publication. Candidate
        # pairs are joined by both IDs, then ranked deterministically by usable
        # contact, fit, and recency. This avoids combining a newer duplicate's
        # contact/body with another publication's match decision.
        latest_per_source = (
            select(
                MatchEvaluation.source_job_id.label("source_job_id"),
                func.max(MatchEvaluation.created_at).label("created_at"),
            )
            .where(
                MatchEvaluation.canonical_job_id == canonical_job_id,
                MatchEvaluation.profile_id == profile_id,
            )
            .group_by(MatchEvaluation.source_job_id)
            .subquery()
        )
        candidates = list(
            (
                await session.execute(
                    select(MatchEvaluation, SourceJob)
                    .join(
                        latest_per_source,
                        and_(
                            latest_per_source.c.source_job_id == MatchEvaluation.source_job_id,
                            latest_per_source.c.created_at == MatchEvaluation.created_at,
                        ),
                    )
                    .join(
                        SourceJob,
                        and_(
                            SourceJob.id == MatchEvaluation.source_job_id,
                            SourceJob.canonical_job_id == MatchEvaluation.canonical_job_id,
                        ),
                    )
                    .where(
                        MatchEvaluation.canonical_job_id == canonical_job_id,
                        MatchEvaluation.profile_id == profile_id,
                        SourceJob.status == JobStatus.ACTIVE,
                    )
                    .order_by(
                        case(
                            (SourceJob.public_email.is_not(None), 0),
                            (SourceJob.application_url.is_not(None), 1),
                            else_=2,
                        ),
                        desc(MatchEvaluation.overall_fit),
                        desc(MatchEvaluation.created_at),
                        MatchEvaluation.id,
                    )
                )
            ).all()
        )
        if not candidates:
            raise ApplicationPreparationError(
                "an active evaluated source publication is required before preparing"
            )

        selected: tuple[MatchEvaluation, SourceJob, Resume, EmployerContact] | None = None
        seen_sources: set[UUID] = set()
        for evaluation, source_job in candidates:
            if source_job.id in seen_sources:
                continue
            seen_sources.add(source_job.id)
            if source_job.raw_metadata.get("incomplete") is True:
                continue
            if not await evaluation_is_current(session, evaluation, source_job):
                continue
            resume = await self.resume_service.select_for_job(session, profile_id, source_job)
            if resume is None:
                continue
            if not evaluation_inputs_are_current(
                evaluation,
                profile,
                preferences,
                resume,
            ):
                continue
            await self.employer_identity.resolve_for_source_job(session, source_job)
            contact = await self.contact_service.discover_from_source_job(session, source_job)
            if contact is None:
                continue
            if require_employer_slot:
                probe = existing or Application(
                    id=uuid4(),
                    profile_id=profile_id,
                    canonical_job_id=canonical_job_id,
                    employer_id=source_job.employer_id,
                    status=ApplicationStatus.PREPARED,
                )
                outcome = await EmployerRelationshipService().policy_outcome(
                    session,
                    application=probe,
                    evaluation=evaluation,
                    job=source_job,
                    max_active_applications=1,
                    freeze_active_conversation=True,
                    rank_candidates=False,
                )
                target = await daily_target_state(session, preferences)
                normal = evaluation.decision is MatchDecision.AUTO_APPLY and (
                    evaluation.overall_fit >= preferences.minimum_auto_send_score
                )
                soft = target.remaining > 0 and catchup_stage(evaluation, preferences) is not None
                if (
                    not outcome.slot_available
                    or not outcome.not_suppressed
                    or source_job.employer_id in target.sent_employers
                    or normalize_for_fingerprint(source_job.company)
                    in target.sent_companies | target.reserved_companies
                    or not (normal or soft)
                    or evaluation.missing_requirements
                    or evaluation.scam_indicators
                ):
                    continue
            selected = evaluation, source_job, resume, contact
            break
        if selected is None:
            raise ApplicationPreparationError(
                "no current evaluated publication has a usable resume and public contact"
            )
        evaluation, source_job, resume, contact = selected
        subject, body, language, used_facts = generate_letter(profile, source_job)
        content_valid = not detect_prompt_injection(body) and source_job.title in subject
        if source_job.company:
            content_valid = content_valid and source_job.company in body
        if existing is None:
            application = Application(
                profile_id=profile_id,
                canonical_job_id=canonical_job_id,
                employer_id=source_job.employer_id,
                source_job_id=source_job.id,
                match_evaluation_id=evaluation.id,
                resume_id=resume.id,
                recipient_contact_id=contact.id,
                subject=subject,
                body=body,
                language=language,
                status=ApplicationStatus.PREPARED,
                idempotency_key=stable_hash("application", str(profile_id), str(canonical_job_id)),
                used_confirmed_facts=used_facts,
                content_validated=content_valid and contact.contact_type == ContactType.EMAIL,
            )
            session.add(application)
        else:
            # Re-prepare the same logical application after a content revision.
            # The row and idempotency key stay stable while old evaluations remain
            # append-only history. Manual approval never carries over implicitly.
            application = existing
            application.source_job_id = source_job.id
            application.employer_id = source_job.employer_id
            application.match_evaluation_id = evaluation.id
            application.resume_id = resume.id
            application.recipient_contact_id = contact.id
            application.subject = subject
            application.body = body
            application.language = language
            application.status = ApplicationStatus.PREPARED
            application.policy_decision = None
            application.policy_result = {}
            application.used_confirmed_facts = used_facts
            application.content_validated = (
                content_valid and contact.contact_type == ContactType.EMAIL
            )
        await session.flush()
        await self.policy_engine.apply(
            session,
            application,
            preferences,
            evaluation,
            source_job,
            resume,
            contact,
            profile,
        )
        if application.employer_id is not None:
            await enqueue_employer_policy_refresh(
                session,
                profile_id=application.profile_id,
                employer_id=application.employer_id,
                reason="application_prepared",
            )
        return application

    async def reevaluate_policy(
        self, session: AsyncSession, application: Application
    ) -> Application:
        refreshed = await session.scalar(
            select(Application)
            .where(Application.id == application.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if refreshed is None:
            raise ApplicationPreparationError("application no longer exists")
        application = refreshed
        if application.status in {
            ApplicationStatus.SENT,
            ApplicationStatus.SENDING,
            ApplicationStatus.DELIVERY_UNKNOWN,
        }:
            return application
        evaluation = await session.get(MatchEvaluation, application.match_evaluation_id)
        source_job = await session.get(SourceJob, application.source_job_id)
        resume = await session.get(Resume, application.resume_id)
        contact = await session.get(EmployerContact, application.recipient_contact_id)
        profile = await self.profile_service.get_processing_profile(session, application.profile_id)
        if (
            evaluation is None
            or source_job is None
            or resume is None
            or contact is None
            or profile is None
        ):
            raise ApplicationPreparationError("application bindings are incomplete")
        preferences = await self.profile_service.get_preferences(session, application.profile_id)
        await self.policy_engine.apply(
            session, application, preferences, evaluation, source_job, resume, contact, profile
        )
        return application

    async def approve(self, session: AsyncSession, application_id: UUID) -> Application:
        application = await session.scalar(
            select(Application).where(Application.id == application_id).with_for_update()
        )
        if application is None:
            raise LookupError(f"application {application_id} does not exist")
        if (
            await self.profile_service.get_processing_profile(session, application.profile_id)
            is None
        ):
            raise ApplicationPreparationError("profile or account is not active")
        if application.status not in {
            ApplicationStatus.PENDING_REVIEW,
            ApplicationStatus.PREPARED,
        }:
            raise ApplicationPreparationError(
                "only prepared or pending-review applications can be approved"
            )
        if not application.content_validated:
            raise ApplicationPreparationError("application content has not passed validation")
        policy_result = (
            application.policy_result if isinstance(application.policy_result, dict) else {}
        )
        if policy_result.get("requires_rematch") is True or policy_result.get(
            "safe_stop_reason"
        ) in {
            "match_evaluation_stale",
            "match_evaluation_inputs_stale",
        }:
            raise ApplicationPreparationError("match evaluation is stale")
        source_job = await session.scalar(
            select(SourceJob).where(SourceJob.id == application.source_job_id).with_for_update()
        )
        if source_job is None or source_job.status != JobStatus.ACTIVE:
            raise ApplicationPreparationError("vacancy is no longer active")
        evaluation = await session.get(MatchEvaluation, application.match_evaluation_id)
        if (
            evaluation is None
            or evaluation.profile_id != application.profile_id
            or evaluation.source_job_id != application.source_job_id
            or evaluation.canonical_job_id != application.canonical_job_id
            or not await evaluation_is_current(session, evaluation, source_job)
        ):
            raise ApplicationPreparationError("match evaluation is stale")
        ensure_transition(application.status, ApplicationStatus.APPROVED)
        application.status = ApplicationStatus.APPROVED
        if application.employer_id is not None:
            await enqueue_employer_policy_refresh(
                session,
                profile_id=application.profile_id,
                employer_id=application.employer_id,
                reason="application_approved",
            )
        await session.flush()
        return application

    async def reject(self, session: AsyncSession, application_id: UUID) -> Application:
        """Cancel an unsent application after an explicit owner decision."""
        application = await session.get(Application, application_id)
        if application is None:
            raise LookupError(f"application {application_id} does not exist")
        try:
            ensure_transition(application.status, ApplicationStatus.CANCELLED)
        except ValueError as exc:
            raise ApplicationPreparationError("only an unsent application can be rejected") from exc
        application.status = ApplicationStatus.CANCELLED
        application.policy_result = {**(application.policy_result or {}), "owner_rejected": True}
        if application.employer_id is not None:
            await enqueue_employer_policy_refresh(
                session,
                profile_id=application.profile_id,
                employer_id=application.employer_id,
                reason="application_cancelled",
            )
        await session.flush()
        return application


async def prepare_pending_applications() -> int:
    """Prepare only missing/stale applications and cheaply refresh transient policy gates."""
    from app.database.session import async_session_factory

    prepared = 0
    settings = get_settings()
    service = ApplicationService(settings)
    refreshable = {
        ApplicationStatus.PREPARED,
        ApplicationStatus.PENDING_REVIEW,
        ApplicationStatus.BLOCKED,
        ApplicationStatus.DEFERRED,
    }
    async with async_session_factory() as session:
        _start_local, recovery_start, recovery_end = local_day_bounds()
        await block_closed_vacancy_applications(
            session,
            actor="application_scheduler",
        )
        ranked = (
            select(
                MatchEvaluation.profile_id.label("profile_id"),
                MatchEvaluation.canonical_job_id.label("canonical_job_id"),
                MatchEvaluation.id.label("evaluation_id"),
                MatchEvaluation.decision.label("decision"),
                MatchEvaluation.created_at.label("evaluation_created_at"),
                MatchEvaluation.overall_fit.label("overall_fit"),
                MatchEvaluation.soft_mismatches.label("soft_mismatches"),
                func.row_number()
                .over(
                    partition_by=(
                        MatchEvaluation.profile_id,
                        MatchEvaluation.canonical_job_id,
                    ),
                    order_by=(MatchEvaluation.created_at.desc(), MatchEvaluation.id.desc()),
                )
                .label("rank"),
            )
        ).subquery()
        rows = (
            await session.execute(
                select(
                    ranked.c.profile_id,
                    ranked.c.canonical_job_id,
                    ranked.c.evaluation_id,
                    Application,
                )
                .outerjoin(
                    Application,
                    and_(
                        Application.profile_id == ranked.c.profile_id,
                        Application.canonical_job_id == ranked.c.canonical_job_id,
                    ),
                )
                .where(
                    ranked.c.rank == 1,
                    or_(
                        Application.id.is_(None),
                        Application.status.in_(refreshable),
                        and_(
                            Application.status == ApplicationStatus.CANCELLED,
                            Application.policy_decision == PolicyDecision.SKIPPED,
                            ranked.c.decision == MatchDecision.SKIP,
                            func.json_array_length(ranked.c.soft_mismatches) > 0,
                            ~select(EmailDelivery.id)
                            .where(EmailDelivery.application_id == Application.id)
                            .exists(),
                        ),
                        and_(
                            Application.status == ApplicationStatus.CANCELLED,
                            Application.policy_decision == PolicyDecision.SKIPPED,
                            ranked.c.decision == MatchDecision.AUTO_APPLY,
                            ranked.c.evaluation_created_at >= recovery_start,
                            ranked.c.evaluation_created_at < recovery_end,
                            or_(
                                Application.match_evaluation_id.is_(None),
                                Application.match_evaluation_id != ranked.c.evaluation_id,
                            ),
                            ~select(EmailDelivery.id)
                            .where(EmailDelivery.application_id == Application.id)
                            .exists(),
                        ),
                    ),
                )
                .order_by(
                    ranked.c.overall_fit.desc(),
                    ranked.c.evaluation_created_at,
                    ranked.c.evaluation_id,
                )
            )
        ).all()

        full_refresh: list[tuple[UUID, UUID]] = []
        policy_refresh_candidates: list[Application] = []
        for profile_id, canonical_id, latest_evaluation_id, application in rows:
            if (
                application is None
                or application.status == ApplicationStatus.PREPARED
                or application.match_evaluation_id != latest_evaluation_id
            ):
                full_refresh.append((profile_id, canonical_id))
            elif _policy_only_refresh_needed(application):
                policy_refresh_candidates.append(application)

        policy_refresh = policy_refresh_candidates
        refresh_limit = settings.application_policy_refresh_batch_size
        if len(policy_refresh_candidates) > refresh_limit:
            cycle = int(time.time() // 300)
            start = (cycle * refresh_limit) % len(policy_refresh_candidates)
            end = start + refresh_limit
            policy_refresh = policy_refresh_candidates[start:end]
            if len(policy_refresh) < refresh_limit:
                policy_refresh.extend(
                    policy_refresh_candidates[: refresh_limit - len(policy_refresh)]
                )

        full_refresh_keys = set(full_refresh)
        policy_refresh_ids = {application.id for application in policy_refresh}
        selected_companies: set[tuple[UUID, str]] = set()
        # Rows are already ordered by fit, so a cheaper policy refresh must not
        # take the target slot before a better, newly evaluated publication.
        for profile_id, canonical_id, _evaluation_id, existing in rows:
            needs_full_refresh = (profile_id, canonical_id) in full_refresh_keys
            if not needs_full_refresh and (
                existing is None or existing.id not in policy_refresh_ids
            ):
                continue
            if (
                existing is not None
                and (existing.policy_result or {}).get("owner_rejected") is True
            ):
                continue
            try:
                canonical = await session.get(CanonicalJob, canonical_id)
                company_key = (
                    normalize_for_fingerprint(canonical.normalized_company) if canonical else ""
                )
                if company_key and (profile_id, company_key) in selected_companies:
                    continue
                before_evaluation = existing.match_evaluation_id if existing is not None else None
                before_policy = existing.policy_decision if existing is not None else None
                if needs_full_refresh:
                    application = await service.prepare(
                        session, canonical_id, profile_id, require_employer_slot=True
                    )
                    if (
                        before_evaluation is None
                        or before_evaluation != application.match_evaluation_id
                    ):
                        prepared += 1
                else:
                    assert existing is not None
                    application = await service.reevaluate_policy(session, existing)
                    if before_policy != application.policy_decision:
                        prepared += 1
                if application.status is ApplicationStatus.AUTO_APPROVED and company_key:
                    selected_companies.add((profile_id, company_key))
                if (
                    application.employer_id is not None
                    and before_policy != application.policy_decision
                ):
                    await enqueue_employer_policy_refresh(
                        session,
                        profile_id=application.profile_id,
                        employer_id=application.employer_id,
                        reason="application_policy_changed",
                    )
            except ApplicationPreparationError:
                continue
            finally:
                # Release reservation and application locks before another
                # candidate; the sender locks its application before the quota.
                await session.commit()
        await session.commit()
    return prepared


async def refresh_dirty_deferred_applications(
    *,
    employer_batch_size: int = 50,
) -> dict[str, int]:
    """Re-evaluate one bounded batch of deferred applications for dirty employers."""
    from app.database.session import async_session_factory

    if employer_batch_size <= 0:
        raise ValueError("employer_batch_size must be positive")

    service = ApplicationService(get_settings())
    totals = {"employers": 0, "applications": 0, "changed": 0, "errors": 0}
    async with async_session_factory() as session:
        dirty = list(
            (
                await session.scalars(
                    select(ApplicationPolicyRefreshQueue)
                    .order_by(
                        ApplicationPolicyRefreshQueue.enqueued_at,
                        ApplicationPolicyRefreshQueue.id,
                    )
                    .limit(employer_batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for marker in dirty:
            applications = list(
                (
                    await session.scalars(
                        select(Application)
                        .where(
                            Application.profile_id == marker.profile_id,
                            Application.employer_id == marker.employer_id,
                            Application.status == ApplicationStatus.DEFERRED,
                            Application.policy_decision == PolicyDecision.DEFERRED,
                        )
                        .order_by(Application.id)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for application in applications:
                before = (application.status, application.policy_decision)
                try:
                    await service.reevaluate_policy(session, application)
                except ApplicationPreparationError:
                    totals["errors"] += 1
                    continue
                totals["applications"] += 1
                if before != (application.status, application.policy_decision):
                    totals["changed"] += 1
            await session.delete(marker)
            totals["employers"] += 1
        await session.commit()
    return totals
