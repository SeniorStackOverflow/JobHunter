from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import JobSnapshot, MatchEvaluation, SourceJob


async def evaluation_is_current(
    session: AsyncSession,
    evaluation: MatchEvaluation,
    job: SourceJob,
) -> bool:
    """Return whether an evaluation describes this exact current publication.

    Content hashes make the normal check deterministic. The snapshot timestamp
    also handles an A -> B -> A content sequence: even though the current hash
    then equals an old hash, the later snapshot still requires a new evaluation.
    """

    if (
        job.canonical_job_id is None
        or evaluation.source_job_id != job.id
        or evaluation.canonical_job_id != job.canonical_job_id
        or evaluation.source_content_hash is None
        or evaluation.source_content_hash != job.content_hash
    ):
        return False
    newer_snapshot = await session.scalar(
        select(JobSnapshot.id)
        .where(
            JobSnapshot.source_job_id == job.id,
            JobSnapshot.timestamp > evaluation.created_at,
        )
        .limit(1)
    )
    return newer_snapshot is None


__all__ = ["evaluation_is_current"]
