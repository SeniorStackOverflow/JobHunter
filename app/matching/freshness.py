from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, cast, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.matching.bindings import preference_fingerprint, profile_fingerprint
from app.matching.hard_requirements import HARD_REQUIREMENT_RULES_VERSION
from app.matching.providers import MATCHING_RULES_VERSION
from app.models.entities import (
    JobPreference,
    JobSnapshot,
    MatchEvaluation,
    Resume,
    SourceJob,
    UserProfile,
)
from app.models.enums import JobStatus
from app.profiles.sources import source_selected_clause
from app.settings import Settings

SAFETY_ONLY_PREVIOUS_RULES = frozenset({"matching-v5"})


async def evaluation_is_current(
    session: AsyncSession,
    evaluation: MatchEvaluation,
    job: SourceJob,
) -> bool:
    """Return whether an evaluation describes current decision-relevant content."""

    if (
        job.canonical_job_id is None
        or evaluation.source_job_id != job.id
        or evaluation.canonical_job_id != job.canonical_job_id
        or evaluation.source_matching_hash is None
        or job.matching_content_hash is None
        or evaluation.source_matching_hash != job.matching_content_hash
    ):
        return False
    newer_relevant_snapshot = await session.scalar(
        select(JobSnapshot.id)
        .where(
            JobSnapshot.source_job_id == job.id,
            JobSnapshot.requires_rematch.is_(True),
            JobSnapshot.timestamp > evaluation.created_at,
        )
        .limit(1)
    )
    return newer_relevant_snapshot is None


def matching_rules_refresh_due(
    evaluation: MatchEvaluation | None,
    *,
    hard_requirement_refresh_due: bool,
) -> bool:
    if evaluation is None or evaluation.prompt_rules_version == MATCHING_RULES_VERSION:
        return False
    return not (
        evaluation.prompt_rules_version in SAFETY_ONLY_PREVIOUS_RULES
        and not hard_requirement_refresh_due
    )


def latest_relevant_snapshot_at() -> Any:
    return (
        select(JobSnapshot.timestamp)
        .where(
            JobSnapshot.source_job_id == SourceJob.id,
            JobSnapshot.requires_rematch.is_(True),
        )
        .order_by(JobSnapshot.timestamp.desc(), JobSnapshot.id.desc())
        .limit(1)
        .correlate(SourceJob)
        .scalar_subquery()
    )


def _resume_stale_clause(resumes: Sequence[Resume]) -> Any:
    if len(resumes) == 0:
        return or_(
            MatchEvaluation.resume_id.is_not(None),
            MatchEvaluation.resume_sha256.is_not(None),
        )
    if len(resumes) == 1:
        current_resume = resumes[0]
        return or_(
            MatchEvaluation.resume_id.is_(None),
            MatchEvaluation.resume_id != current_resume.id,
            MatchEvaluation.resume_sha256.is_(None),
            MatchEvaluation.resume_sha256 != current_resume.sha256,
        )
    # Multiple active resumes require choose_resume_for_job(), which depends on
    # each SourceJob. Preserve the worker's existing conservative semantics.
    return true()


def build_matching_preselection_query(
    profile: UserProfile,
    preference: JobPreference,
    resumes: Sequence[Resume],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> Any:
    """Return the exact SQL preselection used by the matching worker.

    This is the authoritative definition of actionable matching backlog. Any
    report/dashboard that calls a different predicate will eventually lie.
    """

    current_time = now or datetime.now(UTC)
    latest_snapshot_at = latest_relevant_snapshot_at()
    latest_evaluation = aliased(MatchEvaluation)
    latest_evaluation_id = (
        select(latest_evaluation.id)
        .where(
            latest_evaluation.profile_id == profile.id,
            latest_evaluation.source_job_id == SourceJob.id,
        )
        .order_by(
            latest_evaluation.created_at.desc(),
            latest_evaluation.id.desc(),
        )
        .limit(1)
        .correlate(SourceJob)
        .scalar_subquery()
    )
    retry_before = current_time - timedelta(
        seconds=settings.matching_provider_failure_retry_seconds
    )
    stale = or_(
        MatchEvaluation.id.is_(None),
        MatchEvaluation.canonical_job_id != SourceJob.canonical_job_id,
        MatchEvaluation.source_matching_hash.is_(None),
        MatchEvaluation.source_matching_hash != SourceJob.matching_content_hash,
        latest_snapshot_at > MatchEvaluation.created_at,
        MatchEvaluation.profile_fingerprint.is_(None),
        MatchEvaluation.profile_fingerprint != profile_fingerprint(profile),
        MatchEvaluation.preference_fingerprint.is_(None),
        MatchEvaluation.preference_fingerprint != preference_fingerprint(preference),
        _resume_stale_clause(resumes),
        and_(
            MatchEvaluation.prompt_rules_version != MATCHING_RULES_VERSION,
            ~MatchEvaluation.prompt_rules_version.in_(SAFETY_ONLY_PREVIOUS_RULES),
        ),
        MatchEvaluation.hard_requirement_rules_version.is_(None),
        MatchEvaluation.hard_requirement_rules_version != HARD_REQUIREMENT_RULES_VERSION,
        and_(
            MatchEvaluation.created_at <= retry_before,
            cast(MatchEvaluation.risks, String).like("%llm_provider_failure:%"),
        ),
    )
    return (
        select(
            SourceJob,
            MatchEvaluation,
            latest_snapshot_at.label("snapshot_at"),
        )
        .outerjoin(
            MatchEvaluation,
            MatchEvaluation.id == latest_evaluation_id,
        )
        .where(
            SourceJob.status == JobStatus.ACTIVE,
            SourceJob.canonical_job_id.is_not(None),
            source_selected_clause(profile.id, SourceJob.source_id),
            stale,
        )
        .order_by(
            SourceJob.last_seen_at.desc(),
            SourceJob.id,
            MatchEvaluation.id,
        )
    )


async def active_resumes_for_profile(
    session: AsyncSession,
    profile_id: UUID,
) -> list[Resume]:
    return list(
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


async def count_profile_matching_backlog(
    session: AsyncSession,
    profile: UserProfile,
    preference: JobPreference,
    settings: Settings,
    *,
    resumes: Sequence[Resume] | None = None,
) -> int:
    current_resumes = (
        list(resumes)
        if resumes is not None
        else await active_resumes_for_profile(session, profile.id)
    )
    query = build_matching_preselection_query(
        profile,
        preference,
        current_resumes,
        settings,
    )
    ids = query.with_only_columns(SourceJob.id).order_by(None).subquery()
    return int(await session.scalar(select(func.count()).select_from(ids)) or 0)


async def count_all_matching_backlog(
    session: AsyncSession,
    settings: Settings,
) -> int:
    # Local import avoids coupling the matching query module to profile mutation
    # services at import time.
    from app.profiles.service import ProfileService

    profiles = ProfileService()
    total = 0
    for profile in await profiles.list_processing_profiles(session):
        preference = await profiles.get_preferences(session, profile.id)
        total += await count_profile_matching_backlog(
            session,
            profile,
            preference,
            settings,
        )
    return total


__all__ = [
    "SAFETY_ONLY_PREVIOUS_RULES",
    "active_resumes_for_profile",
    "build_matching_preselection_query",
    "count_all_matching_backlog",
    "count_profile_matching_backlog",
    "evaluation_is_current",
    "latest_relevant_snapshot_at",
    "matching_rules_refresh_due",
]
