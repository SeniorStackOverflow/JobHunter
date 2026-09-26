from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import ApplicationPolicyRefreshQueue


async def enqueue_employer_policy_refresh(
    session: AsyncSession,
    *,
    profile_id: UUID,
    employer_id: UUID,
    reason: str,
) -> None:
    now = datetime.now(UTC)
    normalized_reason = reason[:128]
    values = {
        "profile_id": profile_id,
        "employer_id": employer_id,
        "reason": normalized_reason,
        "enqueued_at": now,
    }
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        pg_statement = pg_insert(ApplicationPolicyRefreshQueue).values(**values)
        pg_statement = pg_statement.on_conflict_do_update(
            index_elements=[
                ApplicationPolicyRefreshQueue.profile_id,
                ApplicationPolicyRefreshQueue.employer_id,
            ],
            set_={"reason": normalized_reason, "enqueued_at": now},
        )
        await session.execute(pg_statement)
    elif dialect == "sqlite":
        sqlite_statement = sqlite_insert(ApplicationPolicyRefreshQueue).values(**values)
        sqlite_statement = sqlite_statement.on_conflict_do_update(
            index_elements=[
                ApplicationPolicyRefreshQueue.profile_id,
                ApplicationPolicyRefreshQueue.employer_id,
            ],
            set_={"reason": normalized_reason, "enqueued_at": now},
        )
        await session.execute(sqlite_statement)
    else:
        row = await session.scalar(
            select(ApplicationPolicyRefreshQueue).where(
                ApplicationPolicyRefreshQueue.profile_id == profile_id,
                ApplicationPolicyRefreshQueue.employer_id == employer_id,
            )
        )
        if row is None:
            session.add(ApplicationPolicyRefreshQueue(**values))
        else:
            row.reason = normalized_reason
            row.enqueued_at = now
    await session.flush()
