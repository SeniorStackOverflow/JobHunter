from __future__ import annotations

from app.models.entities import JobPreference, MatchEvaluation
from app.models.enums import MatchDecision


def minimum_daily_requirement(preference: JobPreference) -> int:
    rules = preference.additional_rules or {}
    try:
        minimum_daily = int(rules.get("minimum_daily_applications", 0))
    except (TypeError, ValueError):
        return 0
    return max(0, min(minimum_daily, preference.maximum_daily_applications))


def minimum_catchup_score(preference: JobPreference) -> int:
    """Return the bounded soft auto-apply threshold while minimum is unmet."""

    rules = preference.additional_rules or {}
    try:
        delta = int(rules.get("minimum_daily_catchup_score_delta", 10))
    except (TypeError, ValueError):
        delta = 10
    delta = max(0, min(delta, 20))
    return max(0, preference.minimum_auto_send_score - delta)


def minimum_catchup_active(preference: JobPreference, sent_today: int) -> bool:
    minimum = minimum_daily_requirement(preference)
    return minimum > 0 and sent_today < minimum


def review_is_safe_catchup_candidate(
    evaluation: MatchEvaluation,
    *,
    threshold: int,
) -> bool:
    """Allow catch-up promotion only for an already LLM-reviewed soft-borderline match."""

    material_risks = [
        risk for risk in (evaluation.risks or []) if risk != "experience_relevance_requires_review"
    ]
    return (
        evaluation.decision is MatchDecision.PREPARE_FOR_REVIEW
        and evaluation.overall_fit >= threshold
        and not evaluation.missing_requirements
        and not material_risks
        and not evaluation.scam_indicators
    )
