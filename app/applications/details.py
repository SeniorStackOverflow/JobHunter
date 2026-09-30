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
    CanonicalEmployer,
    EmailDelivery,
    EmployerContact,
    EmployerRelationship,
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
    employer = (
        await session.get(CanonicalEmployer, application.employer_id)
        if application.employer_id is not None
        else None
    )
    relationship = (
        await session.scalar(
            select(EmployerRelationship).where(
                EmployerRelationship.profile_id == application.profile_id,
                EmployerRelationship.employer_id == application.employer_id,
            )
        )
        if application.employer_id is not None
        else None
    )

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
    safe_stop_reason = policy_result.get("safe_stop_reason")
    if safe_stop_reason is None:
        employer_rule_reasons = {
            "employer_identity_resolved": "identity_unresolved",
            "employer_not_suppressed": "previous_candidate_withdrawal_from_employer",
            "no_candidate_withdrawal": "previous_candidate_withdrawal_from_employer",
            "no_active_employer_conversation": "active_employer_conversation",
            "employer_application_slot_available": "same_employer_application_deferred",
        }
        safe_stop_reason = next(
            (reason for rule, reason in employer_rule_reasons.items() if rule in failed_rules),
            None,
        )

    delivery_detail = _fields(
        delivery,
        "id",
        "provider",
        "provider_message_id",
        "thread_id",
        "rfc_message_id",
        "subject_fingerprint",
        "status",
        "attempt_count",
        "last_attempt_at",
        "final_recipient",
        "smtp_status",
        "failure_class",
        "failure_reason",
        "bounced_at",
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
                "employer_id",
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
        "employer_only_deferred": bool(failed_rules)
        and set(failed_rules)
        <= {
            "no_active_employer_conversation",
            "employer_application_slot_available",
            "distinct_employer_today",
        },
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
            "delivery_state",
            "last_delivery_attempt_at",
            "last_delivery_failure_at",
            "last_smtp_status",
            "failure_count",
            "last_failure_reason",
        ),
        "employer": {
            "id": employer.id if employer is not None else None,
            "name": employer.normalized_name if employer is not None else None,
            "primary_domain": employer.primary_domain if employer is not None else None,
            "relationship": (
                _value(relationship.state) if relationship is not None else "never_contacted"
            ),
            "suppression_scope": (
                _value(relationship.suppression_scope) if relationship is not None else "none"
            ),
            "last_interaction_at": (
                relationship.last_interaction_at if relationship is not None else None
            ),
            "policy": {
                "allowed": not any(
                    item
                    in {
                        "employer_not_suppressed",
                        "no_candidate_withdrawal",
                        "no_active_employer_conversation",
                        "employer_application_slot_available",
                    }
                    for item in failed_rules
                ),
                "reason": safe_stop_reason,
            },
        },
        "delivery": delivery_detail,
    }


__all__ = ["get_application_detail"]
