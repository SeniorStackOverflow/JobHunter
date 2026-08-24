from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit_event
from app.models.entities import Application, CanonicalJob
from app.models.enums import ApplicationStatus, JobStatus, PolicyDecision

_BLOCKABLE_APPLICATION_STATUSES = {
    ApplicationStatus.PREPARED,
    ApplicationStatus.PENDING_REVIEW,
    ApplicationStatus.APPROVED,
    ApplicationStatus.AUTO_APPROVED,
}


def _closed_vacancy_policy_result(application: Application) -> dict[str, object]:
    current = application.policy_result if isinstance(application.policy_result, dict) else {}
    result: dict[str, object] = dict(current)
    passed = [
        item
        for item in current.get("rules_passed", [])
        if isinstance(item, str) and item != "vacancy_active"
    ]
    failed = [item for item in current.get("rules_failed", []) if isinstance(item, str)]
    if "vacancy_active" not in failed:
        failed.append("vacancy_active")
    result.update(
        {
            "decision": PolicyDecision.BLOCKED.value,
            "rules_passed": passed,
            "rules_failed": failed,
            "safe_stop_reason": "vacancy_closed",
        }
    )
    return result


async def block_closed_vacancy_applications(
    session: AsyncSession,
    *,
    actor: str,
    canonical_job_ids: Collection[UUID] | None = None,
) -> int:
    """Block unsent applications whose whole canonical vacancy is closed.

    A single closed publication is insufficient when another source still exposes the
    same canonical vacancy. Provider-attempted and terminal applications are never
    rewritten here.
    """

    query = (
        select(Application)
        .join(CanonicalJob, CanonicalJob.id == Application.canonical_job_id)
        .where(
            CanonicalJob.status == JobStatus.CLOSED,
            Application.status.in_(_BLOCKABLE_APPLICATION_STATUSES),
        )
        .with_for_update()
    )
    if canonical_job_ids is not None:
        if not canonical_job_ids:
            return 0
        query = query.where(Application.canonical_job_id.in_(canonical_job_ids))

    applications = list((await session.scalars(query)).all())
    for application in applications:
        application.status = ApplicationStatus.BLOCKED
        application.policy_decision = PolicyDecision.BLOCKED
        application.policy_result = _closed_vacancy_policy_result(application)
        await record_audit_event(
            session,
            actor=actor,
            action="application.blocked_closed_vacancy",
            entity_type="application",
            entity_id=str(application.id),
            correlation_id=str(application.id),
            decision=ApplicationStatus.BLOCKED.value,
            details={
                "reason": "vacancy_closed",
                "canonical_job_id": str(application.canonical_job_id),
                "source_job_id": str(application.source_job_id),
            },
        )
    await session.flush()
    return len(applications)


__all__ = ["block_closed_vacancy_applications"]
