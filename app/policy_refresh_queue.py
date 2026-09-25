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
    values = {
        "profile_id": profile_id,
        "employer_id": employer_id,
        "reason": reason[:128],
        "enqueued_at": now,
    }
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        statement = pg_insert(ApplicationPolicyRefreshQueue).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                ApplicationPolicyRefreshQueue.profile_id,
                ApplicationPolicyRefreshQueue.employer_id,
            ],
            set_={"reason": values["reason"], "enqueued_at": now},
        )
        await session.execute(statement)
    elif dialect == "sqlite":
        statement = sqlite_insert(ApplicationPolicyRefreshQueue).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                ApplicationPolicyRefreshQueue.profile_id,
                ApplicationPolicyRefreshQueue.employer_id,
            ],
            set_={"reason": values["reason"], "enqueued_at": now},
        )
        await session.execute(statement)
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
            row.reason = values["reason"]
            row.enqueued_at = now
    await session.flush()
