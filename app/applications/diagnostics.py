from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.applications.daily_target import (
    TARGET_POLICY_VERSION,
    daily_target_state,
    minimum_catchup_scores,
    review_is_safe_catchup_candidate,
)
from app.models.entities import (
    Application,
    EmployerInteractionEvent,
    JobPreference,
    MatchEvaluation,
)
from app.models.enums import ApplicationStatus, EmployerInteractionType, PolicyDecision
from app.time_utils import local_day_bounds

_SOFT_RULES = {"overall_score_threshold", "match_auto_apply", "match_not_skipped"}
_EMPLOYER_RULES = {
    "no_active_employer_conversation",
    "employer_application_slot_available",
    "distinct_employer_today",
}
_CONTENT_RULES = {
    "verified_email_contact",
    "contact_verified",
    "letter_validated",
    "resume_active_verified",
}


async def daily_minimum_audit(
    session: AsyncSession,
    profile_id: UUID,
    *,
    day: date | None = None,
    include_replay: bool = True,
) -> dict[str, Any]:
    """Read-only replay of saved evidence; never invent historical policy state.

    Each canonical job contributes at most once. Secondary failures can overlap;
    primary blockers cannot. Replay counts only already validated letters whose
    saved non-soft checks passed, so match counts are not promised sends.

    ``include_replay=False`` returns only the live target (minimum, sent,
    reserved, remaining) and today's catch-up sends. The replay-derived counters
    are then zero and ``replay_computed`` is false; a caller must not present
    them. The panel uses this when it does not show the deficit breakdown.
    """
    preference = await session.scalar(
        select(JobPreference).where(JobPreference.profile_id == profile_id)
    )
    if preference is None:
        raise LookupError("profile preferences are required for a read-only minimum audit")
    local_start, start, end = local_day_bounds(day=day)
    target = await daily_target_state(session, preference, now=start)
    ranked_query = select(
        MatchEvaluation.id.label("id"),
        func.row_number()
        .over(
            partition_by=MatchEvaluation.canonical_job_id,
            order_by=(MatchEvaluation.created_at.desc(), MatchEvaluation.id.desc()),
        )
        .label("rank"),
    ).where(MatchEvaluation.profile_id == profile_id)
    if day is not None:
        ranked_query = ranked_query.where(
            MatchEvaluation.created_at >= start, MatchEvaluation.created_at < end
        )
    ranked = ranked_query.subquery()
    primary = dict.fromkeys(
        ("hard_safety", "content", "category", "employer", "soft_match", "ready", "sent"), 0
    )
    secondary: dict[str, int] = {}
    stage_employers: dict[int, set[UUID]] = {
        threshold: set() for threshold in minimum_catchup_scores(preference)
    }
    employer_only = 0
    free_employers: set[UUID] = set()
    unique_jobs = 0
    if include_replay:
        # One row per evaluated vacancy. Select plain values instead of ORM
        # entities and only the policy keys the replay reads: hydrating whole
        # evaluations and applications (letters, explanations, full policy
        # results) for the profile's history took seconds per call.
        rows = (
            await session.execute(
                select(
                    MatchEvaluation.id,
                    MatchEvaluation.decision,
                    MatchEvaluation.overall_fit,
                    MatchEvaluation.missing_requirements,
                    MatchEvaluation.scam_indicators,
                    MatchEvaluation.soft_mismatches,
                    MatchEvaluation.optional_requirements_missing,
                    MatchEvaluation.risks,
                    Application.id,
                    Application.status,
                    Application.policy_decision,
                    Application.content_validated,
                    Application.match_evaluation_id,
                    Application.employer_id,
                    Application.policy_result["rules_failed"],
                    Application.policy_result["owner_rejected"],
                    cast(Application.policy_result, String).not_in(("{}", "null")),
                )
                .join(ranked, and_(ranked.c.id == MatchEvaluation.id, ranked.c.rank == 1))
                .outerjoin(
                    Application,
                    and_(
                        Application.profile_id == profile_id,
                        Application.canonical_job_id == MatchEvaluation.canonical_job_id,
                    ),
                )
            )
        ).all()
        unique_jobs = len(rows)
        for (
            evaluation_id,
            decision,
            overall_fit,
            missing_requirements,
            scam_indicators,
            soft_mismatches,
            optional_requirements_missing,
            risks,
            application_id,
            status,
            policy_decision,
            content_validated,
            bound_evaluation_id,
            employer_id,
            rules_failed,
            owner_rejected,
            has_policy_result,
        ) in rows:
            if application_id is not None and status is ApplicationStatus.SENT:
                primary["sent"] += 1
                continue
            failed = set(rules_failed) if isinstance(rules_failed, list) else set()
            for rule in failed:
                secondary[rule] = secondary.get(rule, 0) + 1
            if (
                application_id is None
                or not has_policy_result
                or not content_validated
                or bound_evaluation_id != evaluation_id
            ):
                primary["content"] += 1
                continue
            category_rules = {
                "category_allowed_for_auto_send",
                "job_title_allowed_by_preferences",
            }
            hard_failed = failed - _SOFT_RULES - _EMPLOYER_RULES - _CONTENT_RULES - category_rules
            if hard_failed or missing_requirements or scam_indicators:
                primary["hard_safety"] += 1
            elif failed & _CONTENT_RULES:
                primary["content"] += 1
            elif failed & category_rules:
                primary["category"] += 1
            elif failed & _EMPLOYER_RULES:
                primary["employer"] += 1
                if failed <= _EMPLOYER_RULES:
                    employer_only += 1
            elif (
                employer_id is None
                or status
                in {
                    ApplicationStatus.DELIVERY_UNKNOWN,
                    ApplicationStatus.FAILED,
                }
                or (
                    status is ApplicationStatus.CANCELLED
                    and (
                        policy_decision is not PolicyDecision.SKIPPED
                        or owner_rejected
                        or not soft_mismatches
                    )
                )
            ):
                primary["hard_safety"] += 1
            else:
                if employer_id in target.reserved_employers:
                    primary["ready"] += 1
                    continue
                free_employers.add(employer_id)
                candidate = MatchEvaluation(
                    decision=decision,
                    overall_fit=overall_fit,
                    missing_requirements=missing_requirements,
                    scam_indicators=scam_indicators,
                    soft_mismatches=soft_mismatches,
                    optional_requirements_missing=optional_requirements_missing,
                    risks=risks,
                )
                accepted = False
                for threshold, employers in stage_employers.items():
                    if review_is_safe_catchup_candidate(
                        candidate, threshold=threshold, allow_soft_skip=True
                    ):
                        employers.add(employer_id)
                        accepted = True
                primary["ready" if accepted else "soft_match"] += 1
    sent_by_stage: dict[str, int] = {}
    for application in (
        await session.scalars(
            select(Application)
            .where(
                Application.profile_id == profile_id,
                Application.status == ApplicationStatus.SENT,
                Application.sent_at >= start,
                Application.sent_at < end,
            )
            .options(load_only(Application.policy_result, raiseload=True))
        )
    ).all():
        stage = (application.policy_result or {}).get("catchup_stage")
        if stage is not None:
            key = str(stage)
            sent_by_stage[key] = sent_by_stage.get(key, 0) + 1
    seconds_left = max(0, int((end - datetime.now(UTC)).total_seconds()))
    catchup_ids = [
        application.id
        for application in (
            await session.scalars(
                select(Application)
                .where(
                    Application.profile_id == profile_id,
                    Application.status == ApplicationStatus.SENT,
                    Application.sent_at >= datetime.now(UTC) - timedelta(days=30),
                )
                .options(load_only(Application.policy_result, raiseload=True))
            )
        ).all()
        if (application.policy_result or {}).get("catchup_stage") is not None
    ]
    reply_ids: set[UUID] = set()
    interview_ids: set[UUID] = set()
    if catchup_ids:
        events = (
            await session.execute(
                select(
                    EmployerInteractionEvent.application_id, EmployerInteractionEvent.event_type
                ).where(
                    EmployerInteractionEvent.profile_id == profile_id,
                    EmployerInteractionEvent.application_id.in_(catchup_ids),
                )
            )
        ).all()
        for application_id, event_type in events:
            if event_type is EmployerInteractionType.EMPLOYER_REPLIED:
                reply_ids.add(application_id)
            elif event_type in {
                EmployerInteractionType.INTERVIEW_PROPOSED,
                EmployerInteractionType.INTERVIEW_CONFIRMED,
                EmployerInteractionType.INTERVIEW_ATTENDED,
            }:
                interview_ids.add(application_id)
    return {
        "local_date": local_start.date().isoformat(),
        "observed_at": datetime.now(UTC).isoformat(),
        "evidence_scope": "saved_current_policy_for_day_evaluations"
        if day
        else "current_saved_policy",
        "profile_id": str(profile_id),
        "target_policy_version": TARGET_POLICY_VERSION,
        "minimum": target.minimum,
        "maximum": target.maximum,
        "sent": target.sent,
        "reserved": target.reserved,
        "remaining": target.remaining,
        "confirmed_deficit": max(0, target.minimum - target.sent),
        "primary_blockers": primary,
        "secondary_rules": secondary,
        "replay_computed": include_replay,
        "unique_jobs": unique_jobs,
        "unique_free_employers": len(free_employers),
        "employer_only": employer_only,
        "catchup_sent_by_stage": sent_by_stage,
        "seconds_left_in_day": seconds_left,
        "catchup_quality_30d": {
            "sent_attempts": len(catchup_ids),
            "with_linked_reply": len(reply_ids),
            "with_linked_interview": len(interview_ids),
        },
        "search_urgency": "idle"
        if target.remaining == 0
        else "aggressive"
        if seconds_left <= 14400
        else "normal",
        "replay": [
            {
                "threshold": threshold,
                "unique_ready_employers": len(employers),
                "possible_catchup_sends": min(target.remaining, len(employers)),
                "remaining_deficit": max(0, target.remaining - len(employers)),
            }
            for threshold, employers in stage_employers.items()
        ],
    }
