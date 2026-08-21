from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contacts import ContactDiscoveryService
from app.crawlers.parsing.normalization import detect_prompt_injection, stable_hash
from app.matching.bindings import evaluation_inputs_are_current
from app.matching.freshness import evaluation_is_current
from app.models.entities import (
    Application,
    CanonicalJob,
    EmailDelivery,
    EmployerContact,
    MatchEvaluation,
    Resume,
    SourceJob,
    UserProfile,
)
from app.models.enums import ApplicationStatus, ContactType, JobStatus
from app.policies import PolicyEngine
from app.profiles import ProfileService, ResumeService
from app.settings import Settings, get_settings


class ApplicationPreparationError(ValueError):
    """A safe application cannot be prepared from the persisted state."""


_POLICY_ONLY_REFRESH_RULES = {
    "deployment_emergency_switch_off",
    "auto_send_enabled",
    "global_pause_off",
    "source_healthy",
    "source_actions_enabled",
    "vacancy_active",
    "verified_email_contact",
    "contact_verified",
    "resume_active_verified",
    "daily_limit",
}


def _policy_only_refresh_needed(application: Application) -> bool:
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
            f"С уважением,\n{profile.name}"  # noqa: RUF001 - intentional Cyrillic text
        )
    elif language == "ro":
        subject = f"Candidatură pentru postul „{job.title}”"
        relevance = f" Experiența mea relevantă: {fact}" if fact else ""
        body = (
            f"Bună ziua, echipa {company}!\n\n"
            f"Doresc să candidez pentru postul „{job.title}”.{relevance} "
            "Aș aprecia ocazia de a discuta cerințele și programul.\n\n"
            f"Cu respect,\n{profile.name}"
        )
    else:
        subject = f"Application for {job.title}"
        relevance = f" One confirmed relevant fact is: {fact}" if fact else ""
        body = (
            f"Hello {company} team,\n\n"
            f"I would like to apply for the {job.title} position.{relevance} "
            "I would welcome a conversation about the requirements and working arrangement.\n\n"
            f"Kind regards,\n{profile.name}"
        )
    return subject, body, language, [fact_id] if fact_id else []


class ApplicationService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.profile_service = ProfileService()
        self.resume_service = ResumeService(settings)
        self.contact_service = ContactDiscoveryService()
        self.policy_engine = PolicyEngine(settings)

    async def prepare(
        self, session: AsyncSession, canonical_job_id: UUID, profile_id: UUID | None = None
    ) -> Application:
        profile = await self.profile_service.get_profile(session, profile_id)
        if profile is None:
            raise ApplicationPreparationError("profile is required")
        profile_id = profile.id
        existing = await session.scalar(
            select(Application).where(
                Application.canonical_job_id == canonical_job_id,
                Application.profile_id == profile_id,
            )
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
            attempted_delivery = await session.scalar(
                select(EmailDelivery.id).where(EmailDelivery.application_id == existing.id)
            )
            if attempted_delivery is not None:
                return existing
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
            contact = await self.contact_service.discover_from_source_job(session, source_job)
            if contact is None:
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
        return application

    async def reevaluate_policy(
        self, session: AsyncSession, application: Application
    ) -> Application:
        evaluation = await session.get(MatchEvaluation, application.match_evaluation_id)
        source_job = await session.get(SourceJob, application.source_job_id)
        resume = await session.get(Resume, application.resume_id)
        contact = await session.get(EmployerContact, application.recipient_contact_id)
        profile = await session.get(UserProfile, application.profile_id)
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
        application = await session.get(Application, application_id)
        if application is None:
            raise LookupError(f"application {application_id} does not exist")
        if application.status not in {
            ApplicationStatus.PENDING_REVIEW,
            ApplicationStatus.PREPARED,
        }:
            raise ApplicationPreparationError(
                "only prepared or pending-review applications can be approved"
            )
        if not application.content_validated:
            raise ApplicationPreparationError("application content has not passed validation")
        application.status = ApplicationStatus.APPROVED
        await session.flush()
        return application


async def prepare_pending_applications() -> int:
    """Prepare only missing/stale applications and cheaply refresh transient policy gates."""
    from app.database.session import async_session_factory

    prepared = 0
    service = ApplicationService(get_settings())
    refreshable = {
        ApplicationStatus.PREPARED,
        ApplicationStatus.PENDING_REVIEW,
        ApplicationStatus.BLOCKED,
    }
    async with async_session_factory() as session:
        ranked = (
            select(
                MatchEvaluation.profile_id.label("profile_id"),
                MatchEvaluation.canonical_job_id.label("canonical_job_id"),
                MatchEvaluation.id.label("evaluation_id"),
                func.row_number().over(
                    partition_by=(
                        MatchEvaluation.profile_id,
                        MatchEvaluation.canonical_job_id,
                    ),
                    order_by=(MatchEvaluation.created_at.desc(), MatchEvaluation.id.desc()),
                ).label("rank"),
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
                    or_(Application.id.is_(None), Application.status.in_(refreshable)),
                )
            )
        ).all()

        full_refresh: list[tuple[UUID, UUID]] = []
        policy_refresh: list[Application] = []
        for profile_id, canonical_id, latest_evaluation_id, application in rows:
            if (
                application is None
                or application.status == ApplicationStatus.PREPARED
                or application.match_evaluation_id != latest_evaluation_id
            ):
                full_refresh.append((profile_id, canonical_id))
            elif _policy_only_refresh_needed(application):
                policy_refresh.append(application)

        for application in policy_refresh:
            try:
                before = application.policy_decision
                await service.reevaluate_policy(session, application)
                if before != application.policy_decision:
                    prepared += 1
            except ApplicationPreparationError:
                continue

        for profile_id, canonical_id in full_refresh:
            try:
                existing = await session.scalar(
                    select(Application).where(
                        Application.profile_id == profile_id,
                        Application.canonical_job_id == canonical_id,
                    )
                )
                before = existing.match_evaluation_id if existing is not None else None
                application = await service.prepare(session, canonical_id, profile_id)
                if before is None or before != application.match_evaluation_id:
                    prepared += 1
            except ApplicationPreparationError:
                continue
        await session.commit()
    return prepared
