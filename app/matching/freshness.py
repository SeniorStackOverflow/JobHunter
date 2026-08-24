from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import JobSnapshot, MatchEvaluation, SourceJob


async def evaluation_is_current(
    session: AsyncSession,
    evaluation: MatchEvaluation,
    job: SourceJob,
) -> bool:
    """Return whether an evaluation describes current decision-relevant content.

    The broad source hash remains available for audit and crawler history.
    Matching freshness uses a narrower hash so publication timestamps and other
    technical metadata cannot block owner decisions. A relevant A -> B -> A
    revision is still stale because the intervening snapshot is retained.
    """

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


__all__ = ["evaluation_is_current"]
