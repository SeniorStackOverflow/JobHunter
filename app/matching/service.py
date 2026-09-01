from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawlers.parsing.normalization import normalize_for_fingerprint
from app.matching.bindings import (
    confirmed_fact_hashes,
    evaluation_inputs_are_current,
    preference_fingerprint,
    profile_fingerprint,
)
from app.matching.prefilter import DeterministicPrefilter
from app.matching.providers import (
    MATCHING_RULES_VERSION,
    GeminiCompatibleProvider,
    LLMProvider,
    LLMProviderUnavailable,
    LLMRouterProvider,
    MockProvider,
    OpenAIProvider,
)
from app.matching.schemas import DeterministicFilterResult, MatchRequest, MatchResult
from app.models.entities import (
    Application,
    JobPreference,
    JobSnapshot,
    MatchEvaluation,
    Resume,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    JobStatus,
    MatchDecision,
    PolicyDecision,
)
from app.profiles.service import ProfileService, choose_resume_for_job
from app.settings import Settings, get_settings

_MAX_JOB_FIELD_CHARS = 50_000
_MAX_RESUME_SUMMARY_CHARS = 50_000
_MATCHING_PROVIDER_BACKOFF_KEY = "job-agent:matching:provider-backoff"
logger = structlog.get_logger(__name__)

_PRIORITY_REMATCH_SAFE_STOPS = {
    "match_evaluation_stale",
    "match_evaluation_inputs_stale",
}


def _as_aware(value: datetime) -> datetime:
    """Coerce a possibly-naive timestamp to UTC-aware before comparing it.

    PostgreSQL returns ``DateTime(timezone=True)`` columns as offset-aware,
    but SQLite round-trips them as naive. ``func.max`` aggregates and freshly
    persisted ORM instances can therefore disagree on tzinfo within one query.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _matching_provider_backoff_remaining(settings: Settings) -> int:
    if settings.environment == "test":
        return 0
    client: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        ttl = await client.ttl(_MATCHING_PROVIDER_BACKOFF_KEY)
        return max(0, int(ttl))
    except Exception as exc:
        logger.warning("matching_provider_backoff_read_failed", error_type=type(exc).__name__)
        return 0
    finally:
        await client.aclose()


async def _set_matching_provider_backoff(settings: Settings, retry_after_seconds: int) -> int:
    ttl = max(60, min(int(retry_after_seconds), settings.matching_provider_failure_retry_seconds))
    if settings.environment == "test":
        return ttl
    client: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await client.set(_MATCHING_PROVIDER_BACKOFF_KEY, "1", ex=ttl)
    except Exception as exc:
        logger.warning("matching_provider_backoff_write_failed", error_type=type(exc).__name__)
    finally:
        await client.aclose()
    return ttl


async def _priority_rematch_source_ids(
    session: AsyncSession,
    profile_id: UUID,
) -> set[UUID]:
    latest_relevant_snapshots = (
        select(
            JobSnapshot.source_job_id.label("source_job_id"),
            func.max(JobSnapshot.timestamp).label("snapshot_at"),
        )
        .where(JobSnapshot.requires_rematch.is_(True))
        .group_by(JobSnapshot.source_job_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                Application.source_job_id,
                Application.status,
                Application.policy_decision,
                Application.policy_result,
                MatchEvaluation,
                SourceJob,
                latest_relevant_snapshots.c.snapshot_at,
            )
            .outerjoin(
                MatchEvaluation,
                MatchEvaluation.id == Application.match_evaluation_id,
            )
            .outerjoin(
                SourceJob,
                SourceJob.id == Application.source_job_id,
            )
            .outerjoin(
                latest_relevant_snapshots,
                latest_relevant_snapshots.c.source_job_id == Application.source_job_id,
            )
            .where(
                Application.profile_id == profile_id,
                Application.status.in_(
                    {
                        ApplicationStatus.AUTO_APPROVED,
                        ApplicationStatus.PENDING_REVIEW,
                    }
                ),
            )
        )
    ).all()
    priority: set[UUID] = set()
    for (
        source_job_id,
        status,
        policy_decision,
        policy_result,
        evaluation,
        job,
        snapshot_at,
    ) in rows:
        safe_stop_reason = (
            policy_result.get("safe_stop_reason") if isinstance(policy_result, dict) else None
        )
        evaluation_is_stale = (
            evaluation is None
            or job is None
            or evaluation.profile_id != profile_id
            or evaluation.source_job_id != source_job_id
            or evaluation.canonical_job_id != job.canonical_job_id
            or evaluation.source_matching_hash is None
            or evaluation.source_matching_hash != job.matching_content_hash
            or (
                snapshot_at is not None
                and _as_aware(snapshot_at) > _as_aware(evaluation.created_at)
            )
        )
        if (
            status == ApplicationStatus.AUTO_APPROVED
            or policy_decision == PolicyDecision.AUTO_APPROVED
            or safe_stop_reason in _PRIORITY_REMATCH_SAFE_STOPS
            or evaluation_is_stale
        ):
            priority.add(source_job_id)
    return priority


def _select_matching_batch(
    candidates: list[tuple[UUID, bool, bool]],
    *,
    ai_batch_size: int,
    priority_ai_batch_size: int,
    max_jobs: int,
    backoff_remaining: int,
) -> tuple[list[tuple[UUID, bool]], int, int, int]:
    """Select a bounded batch while reserving AI capacity for safety rematches."""

    ai_deferred = (
        sum(1 for _, needs_ai, _ in candidates if needs_ai) if backoff_remaining > 0 else 0
    )
    priority_candidates = [item for item in candidates if item[2]]
    regular_candidates = [item for item in candidates if not item[2]]
    priority_budget = min(priority_ai_batch_size, ai_batch_size)
    priority_head: list[tuple[UUID, bool, bool]] = []
    priority_tail: list[tuple[UUID, bool, bool]] = []
    priority_ai_queued = 0
    for candidate in priority_candidates:
        if candidate[1] and priority_ai_queued >= priority_budget:
            priority_tail.append(candidate)
            continue
        priority_head.append(candidate)
        priority_ai_queued += int(candidate[1])

    ordered = [*priority_head, *regular_candidates, *priority_tail]
    batch: list[tuple[UUID, bool]] = []
    ai_selected = 0
    priority_selected = 0
    for source_job_id, needs_ai, is_priority in ordered:
        if len(batch) >= max_jobs:
            break
        if needs_ai and backoff_remaining > 0:
            continue
        if needs_ai and ai_selected >= ai_batch_size:
            continue
        batch.append((source_job_id, needs_ai))
        ai_selected += int(needs_ai)
        priority_selected += int(is_priority)
    return batch, ai_selected, ai_deferred, priority_selected


class MatchingConfigurationError(RuntimeError):
    """The selected LLM provider cannot be initialized safely."""


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _extract_named_values(values: list[dict[str, Any]], keys: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value.get("confirmed") is not True:
            continue
        item = next((value.get(key) for key in keys if isinstance(value.get(key), str)), None)
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return _unique(result)


def _truncate(value: str | None, maximum: int = _MAX_JOB_FIELD_CHARS) -> str | None:
    return value[:maximum] if value else None


def _confirmed_records(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pass only explicitly confirmed structured profile records to the LLM."""
    return [dict(value) for value in values if value.get("confirmed") is True]


def build_match_request(
    job: SourceJob,
    profile: UserProfile,
    preference: JobPreference,
    *,
    prefilter: DeterministicFilterResult,
    resume_category: str | None = None,
    resume_summary: str | None = None,
) -> MatchRequest:
    return MatchRequest(
        job_title=job.title,
        company=job.company,
        category=job.category,
        subcategory=job.subcategory,
        location=job.location,
        schedule=job.schedule,
        workplace_type=job.workplace_type,
        salary_text=job.salary_text,
        description=_truncate(job.description),
        requirements=_truncate(job.requirements),
        responsibilities=_truncate(job.responsibilities),
        profile_skills=_unique(profile.skills or []),
        profile_languages=_extract_named_values(
            profile.languages or [], ("language", "name", "code")
        ),
        profile_work_experience=_confirmed_records(profile.work_experience or []),
        profile_education=_confirmed_records(profile.education or []),
        profile_driving_licences=_unique(profile.driving_licences or []),
        confirmed_facts=_extract_named_values(
            profile.confirmed_facts or [],
            ("fact", "statement", "text", "value", "name", "description"),
        ),
        resume_category=resume_category,
        resume_summary=_truncate(resume_summary, _MAX_RESUME_SUMMARY_CHARS),
        preference_context={
            "allowed_categories": preference.allowed_categories or [],
            "forbidden_categories": preference.forbidden_categories or [],
            "allowed_cities": preference.allowed_cities or [],
            "remote_allowed": preference.remote_allowed,
            "minimum_salary": (
                str(preference.minimum_salary) if preference.minimum_salary is not None else None
            ),
            "salary_currency": preference.salary_currency,
            "allowed_schedules": preference.allowed_schedules or [],
            "forbidden_schedules": preference.forbidden_schedules or [],
            "willing_without_experience": preference.willing_without_experience,
            "consider_outside_primary_resume": preference.consider_outside_primary_resume,
            "minimum_auto_send_score": preference.minimum_auto_send_score,
            "language_constraints": preference.language_constraints or [],
        },
        deterministic_context=prefilter,
    )


def _provider_from_settings(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockProvider()
    if not settings.openai_model:
        raise MatchingConfigurationError("an explicit LLM model is required")
    if settings.llm_provider == "openai":
        if settings.openai_api_key is None:
            raise MatchingConfigurationError("OPENAI_API_KEY is required")
        return OpenAIProvider(
            model=settings.openai_model,
            api_key=settings.openai_api_key.get_secret_value(),
        )
    if settings.llm_provider == "llmrouter":
        if settings.llmrouter_api_key is None:
            raise MatchingConfigurationError("LLMROUTER_API_KEY is required")
        return LLMRouterProvider(
            model=settings.openai_model,
            api_key=settings.llmrouter_api_key.get_secret_value(),
            base_url=settings.llmrouter_base_url,
            prefer=settings.llmrouter_prefer,
        )
    if settings.gemini_api_key is None:
        raise MatchingConfigurationError("GEMINI_API_KEY is required")
    return GeminiCompatibleProvider(
        model=settings.openai_model,
        api_key=settings.gemini_api_key.get_secret_value(),
        base_url=settings.gemini_base_url,
    )


def _estimate_resume_fit(
    job: SourceJob,
    profile: UserProfile,
    resume_category: str | None,
) -> int:
    job_text = normalize_for_fingerprint(
        " ".join(
            part
            for part in (
                job.title,
                job.category,
                job.subcategory,
                job.description,
                job.requirements,
            )
            if part
        )
    )
    normalized_resume_category = normalize_for_fingerprint(resume_category)
    normalized_job_categories = {
        normalize_for_fingerprint(value)
        for value in (job.category, job.subcategory, *(job.categories_seen or []))
        if value
    }
    category_match = bool(
        normalized_resume_category and normalized_resume_category in normalized_job_categories
    )
    skills = [normalize_for_fingerprint(skill) for skill in profile.skills or []]
    normalized_skills = [skill for skill in skills if skill]
    matched_skills = sum(1 for skill in normalized_skills if skill in job_text)
    skill_score = round(50 * matched_skills / len(normalized_skills)) if normalized_skills else 0
    category_score = 50 if category_match else 0
    if category_match and not normalized_skills:
        category_score = 75
    return min(100, category_score + skill_score)


async def _minimum_catchup_active(
    session: AsyncSession, preference: JobPreference, profile_id: UUID
) -> bool:
    rules = preference.additional_rules or {}
    if rules.get("force_minimum_daily_applications") is not True:
        return False
    try:
        minimum_daily = max(
            0,
            min(
                int(rules.get("minimum_daily_applications", 0)),
                preference.maximum_daily_applications,
            ),
        )
    except (TypeError, ValueError):
        return False
    if minimum_daily <= 0:
        return False
    start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = await session.scalar(
        select(func.count(Application.id)).where(
            Application.profile_id == profile_id,
            Application.status == ApplicationStatus.SENT,
            Application.sent_at >= start_of_day,
        )
    )
    return int(sent_today or 0) < minimum_daily


async def _select_resume(session: AsyncSession, profile_id: UUID, job: SourceJob) -> Resume | None:
    resumes = list(
        (
            await session.scalars(
                select(Resume).where(
                    Resume.profile_id == profile_id,
                    Resume.active.is_(True),
                    Resume.verified.is_(True),
                )
            )
        ).all()
    )
    return choose_resume_for_job(resumes, job)


def reconcile_match_result(
    deterministic: DeterministicFilterResult,
    llm_result: MatchResult,
    *,
    minimum_auto_send_score: int | None = None,
) -> MatchResult:
    """Preserve hard deterministic constraints while accepting advisory LLM analysis."""

    if not deterministic.eligible_for_ai:
        return deterministic.to_match_result()

    requirements_met = _unique([*deterministic.requirements_met, *llm_result.requirements_met])
    missing_requirements = _unique(
        [*deterministic.missing_requirements, *llm_result.missing_requirements]
    )
    risks = _unique([*deterministic.risks, *llm_result.risks])
    scam_indicators = _unique([*deterministic.scam_indicators, *llm_result.scam_indicators])
    preference_fit = min(deterministic.preference_fit, llm_result.preference_fit)
    resume_fit = llm_result.resume_fit
    resume_weight = 0.2 if deterministic.outside_resume_allowed else 0.45
    overall_fit = round(resume_fit * resume_weight + preference_fit * (1 - resume_weight))
    decision = llm_result.decision
    reason_suffix: str | None = None

    if scam_indicators:
        decision = MatchDecision.BLOCK
        overall_fit = 0
        reason_suffix = "scam indicators force a deterministic block"
    elif missing_requirements and decision is MatchDecision.AUTO_APPLY:
        decision = MatchDecision.PREPARE_FOR_REVIEW
        reason_suffix = "missing requirements prevent automatic application"
    elif (
        any(risk != "experience_relevance_requires_review" for risk in risks)
        and decision is MatchDecision.AUTO_APPLY
    ):
        decision = MatchDecision.PREPARE_FOR_REVIEW
        reason_suffix = "material unresolved risk requires review"
    elif (
        deterministic.outside_resume_allowed
        and decision is MatchDecision.SKIP
        and resume_fit < 50
        and preference_fit >= 70
        and not missing_requirements
    ):
        decision = MatchDecision.PREPARE_FOR_REVIEW
        reason_suffix = "explicit outside-resume preference overrides low resume-fit-only skip"

    material_risks = [risk for risk in risks if risk != "experience_relevance_requires_review"]
    if (
        decision is MatchDecision.PREPARE_FOR_REVIEW
        and minimum_auto_send_score is not None
        and overall_fit >= minimum_auto_send_score
        and not missing_requirements
        and not material_risks
        and not scam_indicators
    ):
        decision = MatchDecision.AUTO_APPLY
        reason_suffix = (
            "review had no unresolved requirements or material risks and final score "
            "clears the configured auto-send threshold"
        )

    reason = llm_result.reason
    if reason_suffix:
        reason = f"{reason}; {reason_suffix}"
    return MatchResult(
        resume_fit=resume_fit,
        preference_fit=preference_fit,
        overall_fit=overall_fit,
        requirements_met=requirements_met,
        missing_requirements=missing_requirements,
        risks=risks,
        scam_indicators=scam_indicators,
        decision=decision,
        reason=reason[:4000],
    )


class MatchingService:
    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider | None = None,
        *,
        prefilter: DeterministicPrefilter | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider or _provider_from_settings(settings)
        self.prefilter = prefilter or DeterministicPrefilter()

    async def evaluate(
        self,
        job: SourceJob,
        preference: JobPreference,
        profile: UserProfile,
        *,
        resume_fit: int,
        resume_category: str | None = None,
        resume_summary: str | None = None,
    ) -> MatchResult:
        deterministic = self.prefilter.evaluate(
            job,
            preference,
            profile,
            resume_fit=resume_fit,
        )
        if not deterministic.eligible_for_ai:
            return deterministic.to_match_result()
        request = build_match_request(
            job,
            profile,
            preference,
            prefilter=deterministic,
            resume_category=resume_category,
            resume_summary=resume_summary,
        )
        llm_result = await self.provider.evaluate(request)
        return reconcile_match_result(
            deterministic,
            llm_result,
            minimum_auto_send_score=preference.minimum_auto_send_score,
        )

    async def analyze(
        self,
        session: AsyncSession,
        source_job_id: UUID,
        profile_id: UUID | None = None,
    ) -> MatchEvaluation:
        # Serialize matching with crawler updates so the evaluation hash and the
        # SourceJob fields are an atomic view of one publication revision.
        job = await session.scalar(
            select(SourceJob).where(SourceJob.id == source_job_id).with_for_update()
        )
        if job is None:
            raise LookupError(f"source job {source_job_id} does not exist")
        if job.canonical_job_id is None:
            raise ValueError("source job must be assigned to a canonical job before analysis")
        profile_service = ProfileService()
        profile = await profile_service.get_profile(session, profile_id)
        if profile is None:
            raise ValueError("a user profile is required before job analysis")
        preference = await profile_service.get_preferences(session, profile.id)

        resume = await _select_resume(session, profile.id, job)
        resume_category = resume.category if resume is not None else None
        resume_fit = _estimate_resume_fit(job, profile, resume_category)
        minimum_catchup_active = await _minimum_catchup_active(session, preference, profile.id)
        if minimum_catchup_active:
            deterministic = self.prefilter.evaluate(job, preference, profile, resume_fit=resume_fit)
            if deterministic.eligible_for_ai:
                result = deterministic.to_match_result().model_copy(
                    update={
                        "reason": (
                            "; ".join(deterministic.reasons)
                            or "deterministic minimum-daily catch-up evaluation"
                        )
                    }
                )
            else:
                result = deterministic.to_match_result()
        else:
            result = await self.evaluate(
                job,
                preference,
                profile,
                resume_fit=resume_fit,
                resume_category=resume_category,
            )
        evaluation = MatchEvaluation(
            profile_id=profile.id,
            canonical_job_id=job.canonical_job_id,
            source_job_id=job.id,
            resume_fit=result.resume_fit,
            preference_fit=result.preference_fit,
            overall_fit=result.overall_fit,
            requirements_met=result.requirements_met,
            missing_requirements=result.missing_requirements,
            risks=result.risks,
            scam_indicators=result.scam_indicators,
            explanation=result.reason,
            decision=result.decision,
            model=self.provider.model_name,
            prompt_rules_version=MATCHING_RULES_VERSION,
            source_content_hash=job.content_hash,
            source_matching_hash=job.matching_content_hash,
            resume_id=resume.id if resume is not None else None,
            resume_sha256=resume.sha256 if resume is not None else None,
            profile_fingerprint=profile_fingerprint(profile),
            preference_fingerprint=preference_fingerprint(preference),
            confirmed_fact_hashes=confirmed_fact_hashes(profile),
        )
        session.add(evaluation)
        await session.flush()
        return evaluation


async def process_unprocessed_jobs() -> int:
    """Append profile-scoped evaluations for new jobs and content/profile revisions."""

    from app.database.session import async_session_factory

    settings = get_settings()
    backoff_remaining = await _matching_provider_backoff_remaining(settings)
    service = MatchingService(settings)
    processed = 0
    async with async_session_factory() as session:
        profile_service = ProfileService()
        profiles = await profile_service.list_profiles(session)
        if not profiles:
            logger.warning("job_matching_skipped", error_type="MissingUserProfile")
            return 0

        latest_snapshots = (
            select(
                JobSnapshot.source_job_id.label("source_job_id"),
                func.max(JobSnapshot.timestamp).label("snapshot_at"),
            )
            .where(JobSnapshot.requires_rematch.is_(True))
            .group_by(JobSnapshot.source_job_id)
            .subquery()
        )

        for profile in profiles:
            preference = await profile_service.get_preferences(session, profile.id)
            priority_source_ids = await _priority_rematch_source_ids(session, profile.id)
            resumes = list(
                (
                    await session.scalars(
                        select(Resume).where(
                            Resume.profile_id == profile.id,
                            Resume.active.is_(True),
                            Resume.verified.is_(True),
                        )
                    )
                ).all()
            )
            latest_evaluations = (
                select(
                    MatchEvaluation.source_job_id.label("source_job_id"),
                    func.max(MatchEvaluation.created_at).label("evaluated_at"),
                )
                .where(MatchEvaluation.profile_id == profile.id)
                .group_by(MatchEvaluation.source_job_id)
                .subquery()
            )
            rows = (
                await session.execute(
                    select(SourceJob, MatchEvaluation, latest_snapshots.c.snapshot_at)
                    .outerjoin(
                        latest_evaluations,
                        latest_evaluations.c.source_job_id == SourceJob.id,
                    )
                    .outerjoin(
                        MatchEvaluation,
                        and_(
                            MatchEvaluation.profile_id == profile.id,
                            MatchEvaluation.source_job_id == SourceJob.id,
                            MatchEvaluation.created_at == latest_evaluations.c.evaluated_at,
                        ),
                    )
                    .outerjoin(
                        latest_snapshots,
                        latest_snapshots.c.source_job_id == SourceJob.id,
                    )
                    .where(
                        SourceJob.status == JobStatus.ACTIVE,
                        SourceJob.canonical_job_id.is_not(None),
                    )
                    .order_by(SourceJob.last_seen_at.desc(), SourceJob.id, MatchEvaluation.id)
                )
            ).all()
            candidates: list[tuple[UUID, bool, bool]] = []
            seen: set[UUID] = set()
            for job, evaluation, snapshot_at in rows:
                if job.id in seen:
                    continue
                seen.add(job.id)
                resume = choose_resume_for_job(resumes, job)
                retry_due = bool(
                    evaluation is not None
                    and any(
                        risk.startswith("llm_provider_failure:")
                        for risk in (evaluation.risks or [])
                    )
                    and _as_aware(evaluation.created_at)
                    <= datetime.now(UTC)
                    - timedelta(seconds=settings.matching_provider_failure_retry_seconds)
                )
                if (
                    evaluation is None
                    or evaluation.prompt_rules_version != MATCHING_RULES_VERSION
                    or evaluation.source_matching_hash is None
                    or evaluation.source_matching_hash != job.matching_content_hash
                    or (
                        snapshot_at is not None
                        and _as_aware(snapshot_at) > _as_aware(evaluation.created_at)
                    )
                    or not evaluation_inputs_are_current(evaluation, profile, preference, resume)
                    or retry_due
                ):
                    resume_fit = _estimate_resume_fit(
                        job, profile, resume.category if resume else None
                    )
                    needs_ai = service.prefilter.evaluate(
                        job, preference, profile, resume_fit=resume_fit
                    ).eligible_for_ai
                    candidates.append((job.id, needs_ai, job.id in priority_source_ids))

            batch, ai_selected, ai_deferred, priority_selected = _select_matching_batch(
                candidates,
                ai_batch_size=settings.matching_batch_size,
                priority_ai_batch_size=settings.matching_priority_batch_size,
                max_jobs=settings.matching_max_jobs_per_cycle,
                backoff_remaining=backoff_remaining,
            )

            if ai_deferred:
                logger.info(
                    "job_matching_deferred",
                    reason="provider_backoff",
                    retry_after_seconds=backoff_remaining,
                    ai_jobs_deferred=ai_deferred,
                )

            logger.info(
                "job_matching_batch_selected",
                profile_id=str(profile.id),
                candidates=len(candidates),
                batch_size=len(batch),
                ai_batch_size=ai_selected,
                priority_candidates=sum(int(item[2]) for item in candidates),
                priority_selected=priority_selected,
                configured_ai_batch_size=settings.matching_batch_size,
                configured_priority_batch_size=settings.matching_priority_batch_size,
                configured_max_jobs_per_cycle=settings.matching_max_jobs_per_cycle,
            )
            for index, (source_job_id, needs_ai) in enumerate(batch):
                if needs_ai and backoff_remaining > 0:
                    continue
                try:
                    async with session.begin_nested():
                        await service.analyze(session, source_job_id, profile.id)
                except LLMProviderUnavailable as exc:
                    ttl = await _set_matching_provider_backoff(settings, exc.retry_after_seconds)
                    logger.warning(
                        "job_matching_provider_backoff",
                        provider=exc.provider,
                        retry_after_seconds=ttl,
                    )
                    backoff_remaining = ttl
                    await session.commit()
                    continue
                except (LookupError, ValueError) as exc:
                    logger.warning(
                        "job_matching_skipped",
                        profile_id=str(profile.id),
                        source_job_id=str(source_job_id),
                        error_type=type(exc).__name__,
                    )
                    continue
                processed += 1
                if (
                    needs_ai
                    and backoff_remaining <= 0
                    and settings.matching_inter_job_delay_seconds > 0
                    and any(item_needs_ai for _, item_needs_ai in batch[index + 1 :])
                ):
                    await asyncio.sleep(settings.matching_inter_job_delay_seconds)
        await session.commit()
    return processed
