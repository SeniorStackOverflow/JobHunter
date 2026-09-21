from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matching.hard_requirements import (
    HARD_REQUIREMENT_RULES_VERSION,
    HardRequirementEngine,
    hard_requirements_snapshot,
)
from app.matching.schemas import HardRequirementStatus
from app.models.entities import Application, AuditEvent, SourceJob, UserProfile
from app.models.enums import ApplicationStatus, PolicyDecision

_MUTABLE_UNSENT_STATUSES = {
    ApplicationStatus.PREPARED,
    ApplicationStatus.PENDING_REVIEW,
    ApplicationStatus.APPROVED,
    ApplicationStatus.AUTO_APPROVED,
    ApplicationStatus.FAILED,
}
_HISTORICAL_OR_INFLIGHT_STATUSES = {
    ApplicationStatus.SENDING,
    ApplicationStatus.SENT,
    ApplicationStatus.DELIVERY_UNKNOWN,
}


async def audit_application_hard_requirements(
    session: AsyncSession,
    *,
    apply_changes: bool = False,
    profile_id: object | None = None,
) -> dict[str, Any]:
    """Re-evaluate application hard requirements independently of stored LLM matches.

    Unsent applications are downgraded when apply_changes is true. Historical/in-flight
    applications are never rewritten as unsent; they receive an audit annotation only.
    """

    query = (
        select(Application, SourceJob, UserProfile)
        .join(SourceJob, SourceJob.id == Application.source_job_id)
        .join(UserProfile, UserProfile.id == Application.profile_id)
        .order_by(Application.created_at.asc())
    )
    if profile_id is not None:
        query = query.where(Application.profile_id == profile_id)

    rows = (await session.execute(query)).all()
    engine = HardRequirementEngine()
    findings: list[dict[str, Any]] = []
    counts = {
        "checked": 0,
        "findings": 0,
        "missing": 0,
        "unknown": 0,
        "downgraded_to_review": 0,
        "cancelled": 0,
        "historical_flagged": 0,
    }

    for application, job, profile in rows:
        counts["checked"] += 1
        requirements = engine.evaluate(job, profile)
        unresolved = [
            item
            for item in requirements
            if item.status is not HardRequirementStatus.MET
        ]
        if not unresolved:
            continue

        counts["findings"] += 1
        if any(item.status is HardRequirementStatus.MISSING for item in unresolved):
            counts["missing"] += 1
            audit_decision = "missing"
        else:
            counts["unknown"] += 1
            audit_decision = "unknown"

        snapshot = hard_requirements_snapshot(requirements)
        finding = {
            "application_id": str(application.id),
            "source_job_id": str(job.id),
            "title": job.title,
            "company": job.company,
            "application_status": application.status.value,
            "hard_requirements": snapshot,
            "audit_decision": audit_decision,
        }
        findings.append(finding)

        if not apply_changes:
            continue

        policy_result = (
            dict(application.policy_result)
            if isinstance(application.policy_result, dict)
            else {}
        )
        audit_payload = {
            "rules_version": HARD_REQUIREMENT_RULES_VERSION,
            "decision": audit_decision,
            "hard_requirements": snapshot,
        }

        if application.status in _MUTABLE_UNSENT_STATUSES:
            policy_result["hard_requirement_retro_audit"] = audit_payload
            policy_result["safe_stop_reason"] = "hard_requirement_retro_audit"
            policy_result["requires_rematch"] = True
            if audit_decision == "missing":
                application.status = ApplicationStatus.CANCELLED
                application.policy_decision = PolicyDecision.SKIPPED
                counts["cancelled"] += 1
            else:
                application.status = ApplicationStatus.PENDING_REVIEW
                application.policy_decision = PolicyDecision.PENDING_REVIEW
                counts["downgraded_to_review"] += 1
            application.policy_result = policy_result
        elif application.status in _HISTORICAL_OR_INFLIGHT_STATUSES:
            policy_result["post_send_hard_requirement_audit"] = audit_payload
            application.policy_result = policy_result
            counts["historical_flagged"] += 1
        else:
            # BLOCKED/CANCELLED are already safe from delivery. Keep their status but
            # attach the evidence for auditability.
            policy_result["hard_requirement_retro_audit"] = audit_payload
            application.policy_result = policy_result

        session.add(
            AuditEvent(
                actor="system:hard_requirement_audit",
                action="application.hard_requirement_audited",
                entity_type="application",
                entity_id=str(application.id),
                decision=audit_decision,
                sanitized_details={
                    "source_job_id": str(job.id),
                    "status": application.status.value,
                    "requirement_ids": [
                        item.requirement_id for item in unresolved
                    ],
                    "rules_version": HARD_REQUIREMENT_RULES_VERSION,
                },
                correlation_id=str(uuid4()),
            )
        )

    if apply_changes:
        await session.flush()

    return {**counts, "items": findings}


__all__ = ["audit_application_hard_requirements"]
