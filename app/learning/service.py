from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matching.prefilter import canonicalize_location
from app.models.entities import (
    Application,
    MatchEvaluation,
    ReviewFeedbackEvent,
    ReviewLearningSetting,
    SourceJob,
)
from app.models.enums import ReviewOutcome, ReviewReason

FEATURE_SCHEMA_VERSION = "review-v1"
MINIMUM_LABELS = 6
MINIMUM_PER_OUTCOME = 2

_TITLE_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_TITLE_STOP_WORDS = {
    "and",
    "assistant",
    "для",
    "или",
    "менеджер",
    "operator",
    "оператор",
    "работник",
    "specialist",
    "специалист",
    "the",
    "with",
}
_ALL_DIMENSIONS = (
    "category",
    "title",
    "city",
    "schedule",
    "workplace",
    "employment",
    "experience",
    "company",
    "salary",
)
_REJECTION_DIMENSIONS = {
    ReviewReason.ROLE: ("category", "title"),
    ReviewReason.SALARY: ("salary",),
    ReviewReason.SCHEDULE: ("schedule", "employment"),
    ReviewReason.LOCATION: ("city", "workplace"),
    ReviewReason.COMPANY: ("company",),
    ReviewReason.REQUIREMENTS: ("experience",),
    ReviewReason.OTHER: _ALL_DIMENSIONS,
}
_DIMENSION_LABELS = {
    "category": "категория",
    "title": "должность",
    "city": "город",
    "schedule": "график",
    "workplace": "формат работы",
    "employment": "занятость",
    "experience": "опыт",
    "company": "компания",
    "salary": "зарплата",
}
_REASON_LABELS = {
    ReviewReason.ROLE: "Должность",
    ReviewReason.SALARY: "Зарплата",
    ReviewReason.SCHEDULE: "График",
    ReviewReason.LOCATION: "Место",
    ReviewReason.COMPANY: "Компания",
    ReviewReason.REQUIREMENTS: "Требования",
    ReviewReason.VACANCY_PROBLEM: "Проблема вакансии",
    ReviewReason.OTHER: "Другое",
}
_CITY_LABELS = {
    "balti": "Бельцы",
    "chisinau": "Кишинёв",
}


class ReviewLearningError(ValueError):
    """The explicit review decision cannot be recorded consistently."""


@dataclass(frozen=True, slots=True)
class ReviewJobInput:
    title: str
    company: str | None = None
    category: str | None = None
    categories_seen: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    location: str | None = None
    schedule: str | None = None
    workplace_type: str | None = None
    employment_type: str | None = None
    required_experience: str | None = None
    no_experience: bool | None = None
    salary_present: bool = False


@dataclass(frozen=True, slots=True)
class _FeatureStat:
    dimension: str
    value: str
    approved: int = 0
    rejected: int = 0

    @property
    def support(self) -> int:
        return self.approved + self.rejected


@dataclass(frozen=True, slots=True)
class LearningProposal:
    direction: str
    label: str
    support: int
    approved: int
    rejected: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "direction": self.direction,
            "label": self.label,
            "support": self.support,
            "approved": self.approved,
            "rejected": self.rejected,
        }


@dataclass(frozen=True, slots=True)
class LearnedReviewScore:
    value: int
    hint: str | None
    evidence: int

    def as_dict(self) -> dict[str, str | int | None]:
        return {"value": self.value, "hint": self.hint, "evidence": self.evidence}


@dataclass(frozen=True, slots=True)
class ReviewLearningSummary:
    approved: int
    rejected: int
    excluded: int
    influence_enabled: bool
    ready: bool
    labels_needed: int
    status_label: str
    proposals: tuple[LearningProposal, ...]
    _feature_stats: dict[str, _FeatureStat] = field(repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "rejected": self.rejected,
            "excluded": self.excluded,
            "influence_enabled": self.influence_enabled,
            "ready": self.ready,
            "labels_needed": self.labels_needed,
            "status": self.status_label,
            "proposals": [item.as_dict() for item in self.proposals],
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
        }


def review_reason_labels() -> tuple[tuple[str, str], ...]:
    return tuple((reason.value, label) for reason, label in _REASON_LABELS.items())


def _normalized(value: str | None, *, limit: int = 100) -> str | None:
    compact = " ".join((value or "").casefold().split())[:limit]
    return compact or None


def _unique(values: list[str | None]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def fixed_preference_dimensions(allowed_cities: Iterable[str]) -> frozenset[str]:
    """Dimensions fixed by explicit preferences must not be relearned as soft signals."""

    canonical_cities = {
        canonical for value in allowed_cities if (canonical := canonicalize_location(value))
    }
    return frozenset({"city"}) if len(canonical_cities) == 1 else frozenset()


def review_job_input(job: SourceJob) -> ReviewJobInput:
    return ReviewJobInput(
        title=job.title,
        company=job.company,
        category=job.category,
        categories_seen=tuple(job.categories_seen or ()),
        cities=tuple(job.cities or ()),
        location=job.location,
        schedule=job.schedule,
        workplace_type=job.workplace_type,
        employment_type=job.employment_type,
        required_experience=job.required_experience,
        no_experience=job.no_experience,
        salary_present=(
            job.salary_min is not None or job.salary_max is not None or bool(job.salary_text)
        ),
    )


def _feature_snapshot(job: ReviewJobInput) -> dict[str, list[str]]:
    title_terms = _unique(
        [
            _normalized(word, limit=40)
            for word in _TITLE_WORD.findall(job.title)
            if len(word) >= 3 and word.casefold() not in _TITLE_STOP_WORDS
        ]
    )[:8]
    categories = _unique(
        [_normalized(job.category), *(_normalized(item) for item in job.categories_seen)]
    )[:4]
    cities = _unique(
        [
            *(canonicalize_location(item) for item in job.cities),
            canonicalize_location(job.location),
        ]
    )[:4]
    experience = _normalized(job.required_experience)
    if experience is None and job.no_experience is not None:
        experience = "без опыта" if job.no_experience else "опыт требуется"
    return {
        "category": categories,
        "title": title_terms,
        "city": cities,
        "schedule": _unique([_normalized(job.schedule)]),
        "workplace": _unique([_normalized(job.workplace_type)]),
        "employment": _unique([_normalized(job.employment_type)]),
        "experience": _unique([experience]),
        "company": _unique([_normalized(job.company)]),
        "salary": ["указана" if job.salary_present else "не указана"],
    }


def _feature_label(dimension: str, value: str) -> str:
    if dimension == "city":
        value = _CITY_LABELS.get(value, value)
    return f"{_DIMENSION_LABELS.get(dimension, dimension)}: {value}"


def _summarize_events(
    events: list[ReviewFeedbackEvent],
    *,
    influence_enabled: bool,
    ignored_dimensions: Iterable[str] = (),
) -> ReviewLearningSummary:
    approved = sum(
        event.learning_eligible and event.outcome == ReviewOutcome.APPROVED for event in events
    )
    rejected = sum(
        event.learning_eligible and event.outcome == ReviewOutcome.REJECTED for event in events
    )
    excluded = sum(not event.learning_eligible for event in events)
    ignored = set(ignored_dimensions)
    mutable_stats: dict[str, dict[str, Any]] = {}
    for event in events:
        if not event.learning_eligible:
            continue
        raw_features = event.feature_snapshot.get("features", {})
        raw_dimensions = event.feature_snapshot.get("learning_dimensions", [])
        if not isinstance(raw_features, dict) or not isinstance(raw_dimensions, list):
            continue
        dimensions = {str(item) for item in raw_dimensions} - ignored
        for dimension, raw_values in raw_features.items():
            if dimension not in dimensions or not isinstance(raw_values, list):
                continue
            for raw_value in raw_values:
                value = (
                    canonicalize_location(str(raw_value)) if dimension == "city" else str(raw_value)
                )[:100]
                if not value:
                    continue
                key = f"{dimension}\x00{value}"
                entry = mutable_stats.setdefault(
                    key,
                    {"dimension": str(dimension), "value": value, "approved": 0, "rejected": 0},
                )
                entry[event.outcome.value] += 1
    stats = {key: _FeatureStat(**value) for key, value in mutable_stats.items()}
    ready = (
        approved + rejected >= MINIMUM_LABELS
        and approved >= MINIMUM_PER_OUTCOME
        and rejected >= MINIMUM_PER_OUTCOME
    )
    proposals: list[LearningProposal] = []
    for stat in stats.values():
        if stat.support < 3 or max(stat.approved, stat.rejected) / stat.support < 0.75:
            continue
        direction = "чаще принимаете" if stat.approved > stat.rejected else "чаще отклоняете"
        proposals.append(
            LearningProposal(
                direction=direction,
                label=_feature_label(stat.dimension, stat.value),
                support=stat.support,
                approved=stat.approved,
                rejected=stat.rejected,
            )
        )
    proposals.sort(key=lambda item: (-item.support, item.label))
    total = approved + rejected
    labels_needed = max(
        0,
        MINIMUM_LABELS - total,
        max(0, MINIMUM_PER_OUTCOME - approved) + max(0, MINIMUM_PER_OUTCOME - rejected),
    )
    if not influence_enabled:
        status_label = "приостановлено"
    elif ready:
        status_label = "учитывает решения"
    else:
        status_label = "собирает примеры"
    return ReviewLearningSummary(
        approved=approved,
        rejected=rejected,
        excluded=excluded,
        influence_enabled=influence_enabled,
        ready=ready,
        labels_needed=labels_needed,
        status_label=status_label,
        proposals=tuple(proposals[:3]),
        _feature_stats=stats,
    )


def _score(summary: ReviewLearningSummary, job: ReviewJobInput) -> LearnedReviewScore | None:
    if not summary.ready or not summary.influence_enabled:
        return None
    total = summary.approved + summary.rejected
    baseline = (summary.approved + 1) / (total + 2)
    dimension_factors: list[tuple[float, _FeatureStat]] = []
    for dimension, values in _feature_snapshot(job).items():
        options: list[tuple[float, _FeatureStat]] = []
        for value in values:
            stat = summary._feature_stats.get(f"{dimension}\x00{value}")
            if stat is None or stat.support < 2:
                continue
            acceptance = (stat.approved + 1) / (stat.support + 2)
            delta = (acceptance - baseline) * min(1.0, stat.support / 5)
            options.append((delta, stat))
        if options:
            dimension_factors.append(max(options, key=lambda item: abs(item[0])))
    if not dimension_factors:
        return LearnedReviewScore(value=round(baseline * 100), hint=None, evidence=0)
    combined = sum(delta for delta, _stat in dimension_factors) / len(dimension_factors)
    score_value = min(95, max(5, round(baseline * 100 + combined * 80)))
    strongest_positive = max(dimension_factors, key=lambda item: item[0])
    strongest_negative = min(dimension_factors, key=lambda item: item[0])
    if strongest_negative[0] < 0 and abs(strongest_negative[0]) >= strongest_positive[0] * 0.8:
        strongest_delta, strongest = strongest_negative
    else:
        strongest_delta, strongest = strongest_positive
    tendency = "чаще принимали" if strongest_delta >= 0 else "чаще отклоняли"
    hint = f"Раньше вы {tendency}: {_feature_label(strongest.dimension, strongest.value)}"
    return LearnedReviewScore(value=score_value, hint=hint, evidence=strongest.support)


class ReviewLearningService:
    async def record_decision(
        self,
        session: AsyncSession,
        application: Application,
        *,
        outcome: ReviewOutcome,
        actor: str,
        reason: ReviewReason | None = None,
        reason_text: str = "",
        learn: bool = True,
    ) -> ReviewFeedbackEvent:
        existing = await session.scalar(
            select(ReviewFeedbackEvent).where(ReviewFeedbackEvent.application_id == application.id)
        )
        if existing is not None:
            if existing.outcome != outcome:
                raise ReviewLearningError("a different review decision is already recorded")
            return existing
        if outcome == ReviewOutcome.REJECTED and reason is None:
            reason = ReviewReason.OTHER
        job = await session.get(SourceJob, application.source_job_id)
        evaluation = await session.get(MatchEvaluation, application.match_evaluation_id)
        if job is None:
            raise ReviewLearningError("review job does not exist")
        learning_eligible = learn
        exclusion_reason: str | None = None
        if not learn:
            learning_eligible = False
            exclusion_reason = "operator_opt_out"
        elif reason == ReviewReason.VACANCY_PROBLEM:
            learning_eligible = False
            exclusion_reason = "vacancy_problem"
        elif evaluation is None:
            learning_eligible = False
            exclusion_reason = "missing_evaluation"
        elif (
            evaluation.profile_id != application.profile_id
            or evaluation.source_job_id != application.source_job_id
            or evaluation.canonical_job_id != application.canonical_job_id
            or evaluation.source_content_hash != job.content_hash
            or evaluation.resume_id != application.resume_id
        ):
            learning_eligible = False
            exclusion_reason = "stale_or_mismatched_snapshot"
        dimensions = (
            _ALL_DIMENSIONS
            if outcome == ReviewOutcome.APPROVED
            else _REJECTION_DIMENSIONS.get(reason or ReviewReason.OTHER, ())
        )
        snapshot = {
            "features": _feature_snapshot(review_job_input(job)),
            "learning_dimensions": list(dimensions),
        }
        event = ReviewFeedbackEvent(
            profile_id=application.profile_id,
            application_id=application.id,
            match_evaluation_id=application.match_evaluation_id,
            canonical_job_id=application.canonical_job_id,
            source_job_id=application.source_job_id,
            outcome=outcome,
            reason_code=reason,
            reason_text=reason_text.strip()[:500] or None,
            actor=actor[:255],
            learning_eligible=learning_eligible,
            exclusion_reason=exclusion_reason,
            source_content_hash=job.content_hash,
            profile_fingerprint=evaluation.profile_fingerprint if evaluation else None,
            preference_fingerprint=evaluation.preference_fingerprint if evaluation else None,
            resume_sha256=evaluation.resume_sha256 if evaluation else None,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            feature_snapshot=snapshot,
        )
        session.add(event)
        await session.flush()
        return event

    async def summary(
        self,
        session: AsyncSession,
        profile_id: UUID,
        *,
        ignored_dimensions: Iterable[str] = (),
    ) -> ReviewLearningSummary:
        setting = await session.scalar(
            select(ReviewLearningSetting).where(ReviewLearningSetting.profile_id == profile_id)
        )
        events = list(
            (
                await session.scalars(
                    select(ReviewFeedbackEvent)
                    .where(ReviewFeedbackEvent.profile_id == profile_id)
                    .order_by(desc(ReviewFeedbackEvent.created_at))
                )
            ).all()
        )
        return _summarize_events(
            events,
            influence_enabled=setting.influence_enabled if setting is not None else True,
            ignored_dimensions=ignored_dimensions,
        )

    async def set_influence(
        self,
        session: AsyncSession,
        profile_id: UUID,
        *,
        enabled: bool,
        ignored_dimensions: Iterable[str] = (),
    ) -> ReviewLearningSummary:
        setting = await session.scalar(
            select(ReviewLearningSetting).where(ReviewLearningSetting.profile_id == profile_id)
        )
        if setting is None:
            setting = ReviewLearningSetting(profile_id=profile_id, influence_enabled=enabled)
            session.add(setting)
        else:
            setting.influence_enabled = enabled
        await session.flush()
        return await self.summary(
            session,
            profile_id,
            ignored_dimensions=ignored_dimensions,
        )

    def score(
        self, summary: ReviewLearningSummary, job: SourceJob | ReviewJobInput
    ) -> LearnedReviewScore | None:
        return _score(summary, review_job_input(job) if isinstance(job, SourceJob) else job)
