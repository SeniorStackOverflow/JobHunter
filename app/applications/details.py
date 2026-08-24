from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.reconciliation import (
    delivery_is_stale,
    delivery_reconcile_available_at,
)
from app.matching.freshness import evaluation_is_current
from app.models.entities import (
    Application,
    EmailDelivery,
    EmployerContact,
    JobSource,
    MatchEvaluation,
    Resume,
    SourceJob,
)


def _value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _fields(item: Any, *names: str) -> dict[str, Any] | None:
    if item is None:
        return None
    return {name: _value(getattr(item, name)) for name in names}


async def get_application_detail(session: AsyncSession, application_id: UUID) -> dict[str, Any]:
    """Return the protected review view without storage paths or secret provider data."""

    application = await session.get(Application, application_id)
    if application is None:
        raise LookupError(f"application {application_id} does not exist")
    job = await session.get(SourceJob, application.source_job_id)
    evaluation = (
        await session.get(MatchEvaluation, application.match_evaluation_id)
        if application.match_evaluation_id is not None
        else None
    )
    resume = await session.get(Resume, application.resume_id)
    contact = await session.get(EmployerContact, application.recipient_contact_id)
    delivery = await session.scalar(
        select(EmailDelivery).where(EmailDelivery.application_id == application.id)
    )
    source = await session.get(JobSource, job.source_id) if job is not None else None

    if (
        job is None
        or evaluation is None
        or evaluation.profile_id != application.profile_id
        or evaluation.source_job_id != application.source_job_id
        or evaluation.canonical_job_id != application.canonical_job_id
    ):
        match_evaluation_issue = "invalid_match_evaluation_binding"
    elif not await evaluation_is_current(session, evaluation, job):
        match_evaluation_issue = "match_evaluation_stale"
    else:
        match_evaluation_issue = None

    policy_result = application.policy_result if isinstance(application.policy_result, dict) else {}
    failed_rules = policy_result.get("rules_failed", [])
    if not isinstance(failed_rules, list):
        failed_rules = []

    delivery_detail = _fields(
        delivery,
        "id",
        "provider",
        "provider_message_id",
        "thread_id",
        "status",
        "attempt_count",
        "created_at",
        "updated_at",
    )
    if delivery_detail is not None and delivery is not None:
        delivery_detail["can_reconcile_unknown"] = (
            _value(application.status) == "sending"
            and _value(delivery.status) == "sending"
            and delivery_is_stale(delivery)
        )
        delivery_detail["reconcile_available_at"] = delivery_reconcile_available_at(delivery)

    return {
        **(
            _fields(
                application,
                "id",
                "canonical_job_id",
                "source_job_id",
                "resume_id",
                "recipient_contact_id",
                "subject",
                "body",
                "language",
                "status",
                "policy_decision",
                "policy_result",
                "used_confirmed_facts",
                "content_validated",
                "created_at",
                "sent_at",
            )
            or {}
        ),
        "match_evaluation_issue": match_evaluation_issue,
        "failed_policy_rules": [str(item) for item in failed_rules],
        "job": _fields(
            job,
            "id",
            "source_id",
            "title",
            "company",
            "canonical_url",
            "category",
            "location",
            "status",
            "description",
            "requirements",
            "responsibilities",
            "salary_text",
            "schedule",
            "employment_type",
            "required_experience",
            "workplace_type",
            "last_seen_at",
            "last_checked_at",
            "confirmed_absence_count",
        ),
        "source": _fields(source, "id", "name", "adapter_type", "health_status"),
        "resume": _fields(
            resume,
            "id",
            "name",
            "category",
            "original_filename",
            "mime_type",
            "sha256",
            "active",
            "verified",
            "is_default",
        ),
        "contact": _fields(
            contact,
            "id",
            "value",
            "contact_type",
            "discovery_source",
            "official_domain",
            "verification_status",
            "confidence",
            "evidence_url",
        ),
        "delivery": delivery_detail,
    }


__all__ = ["get_application_detail"]
