from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matching.freshness import evaluation_is_current
from app.matching.prefilter import canonicalize_location
from app.models.entities import (
    Application,
    MatchEvaluation,
    Resume,
    ReviewFeedbackEvent,
    ReviewLearningSetting,
    SourceJob,
)
from app.models.enums import ReviewOutcome, ReviewReason

FEATURE_SCHEMA_VERSION = "review-v2"
LEGACY_FEATURE_SCHEMA_VERSION = "review-v1"
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
    "area",
    "schedule",
    "workplace",
    "experience",
    "company",
    "salary",
)
_LEGACY_COMPATIBLE_DIMENSIONS = frozenset(
    {
        "category",
        "title",
        "city",
        "area",
        "schedule",
        "workplace",
        "experience",
        "company",
    }
)
_SCHEMA_DIMENSIONS = {
    LEGACY_FEATURE_SCHEMA_VERSION: _LEGACY_COMPATIBLE_DIMENSIONS,
    FEATURE_SCHEMA_VERSION: frozenset(_ALL_DIMENSIONS),
}
_RESUME_DEPENDENT_DIMENSIONS = frozenset({"category", "title", "experience"})
_REJECTION_DIMENSIONS = {
    ReviewReason.ROLE: ("category", "title"),
    ReviewReason.SALARY: ("salary",),
    ReviewReason.SCHEDULE: ("schedule",),
    ReviewReason.LOCATION: ("city", "area", "workplace"),
    ReviewReason.COMPANY: ("company",),
    ReviewReason.REQUIREMENTS: ("experience",),
    # An unstructured rejection is a real owner decision, but it is not evidence
    # that every visible attribute caused the rejection.
    ReviewReason.OTHER: (),
}
_DIMENSION_LABELS = {
    "category": "категория",
    "title": "должность",
    "city": "город",
    "area": "район",
    "schedule": "график",
    "workplace": "формат работы",
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
_SCHEDULE_LABELS = {
    "flexible": "гибкий",
    "full_time": "полный день",
    "part_time": "частичная занятость",
    "shifts": "сменный",
}
_SALARY_LABELS = {
    "missing": "не указана",
    "numeric": "указана числом",
    "textual": "указана без суммы",
}

_NO_EXPERIENCE = re.compile(
    r"\bno\s+experience\b|\bwithout\s+experience\b|"
    r"без\s+опыта|опыт\s+не\s+требуется|f[ăa]r[ăa]\s+experien[țt][ăa]",
    re.IGNORECASE,
)
_REMOTE_LOCATION = re.compile(
    r"\bremote\b|удал\w*\s*н|distan[țt][ăa]|distanta",
    re.IGNORECASE,
)
_MISSING_SALARY = re.compile(
    r"^(?:не\s+указан[ао]?|not\s+(?:specified|provided)|"  # noqa: RUF001
    r"nespecificat[ăa]?|nu\s+este\s+specificat[ăa]?)\.?$",
    re.IGNORECASE,
)


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
    salary_text: str | None = None
    salary_min: Decimal | None = None
    salary_max: Decimal | None = None


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
class _DimensionStat:
    approved: int = 0
    rejected: int = 0

    @property
    def support(self) -> int:
        return self.approved + self.rejected

    @property
    def ready(self) -> bool:
        return (
            self.support >= MINIMUM_LABELS
            and self.approved >= MINIMUM_PER_OUTCOME
            and self.rejected >= MINIMUM_PER_OUTCOME
        )


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
    _dimension_stats: dict[str, _DimensionStat] = field(repr=False)

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
        salary_text=job.salary_text,
        salary_min=job.salary_min,
        salary_max=job.salary_max,
    )


def _canonical_schedule(value: str | None) -> str | None:
    normalized = _normalized(value)
    if normalized is None:
        return None
    if any(marker in normalized for marker in ("частичная занятость", "part-time", "part time")):
        return "part_time"
    if any(
        marker in normalized
        for marker in ("по сменному графику", "сменн", "în ture", "in ture", "shift")
    ):
        return "shifts"
    if any(marker in normalized for marker in ("гибк", "свободный график", "flexibil")):
        return "flexible"
    if normalized in {"полный день", "full-time", "full time"}:
        return "full_time"
    return normalized


def _canonical_experience(value: str | None, no_experience: bool | None = None) -> str | None:
    normalized = _normalized(value)
    if normalized is not None:
        if _NO_EXPERIENCE.search(normalized):
            return "без опыта"
        if "более 5" in normalized or re.search(r"\b(?:over|peste)\s+5\b", normalized):
            return "от 5 лет"
        years = re.search(
            # The Cyrillic alternatives and en dash are intentional source text.
            r"(?:\bот\s*|\bmin(?:im(?:um)?)?\s*|\bat\s+least\s*)"  # noqa: RUF001
            r"([1-5])(?:\s*[-–]\s*\d+)?\s*(?:лет|год|ani|years?)?",  # noqa: RUF001
            normalized,
        )
        if years:
            return f"от {years.group(1)} лет"
        if normalized == "до года":
            return "до года"
        if normalized in {
            "с опытом",  # noqa: RUF001
            "experience required",
            "cu experiență",
            "cu experienta",
        }:
            return "опыт требуется"
        return normalized
    if no_experience is not None:
        return "без опыта" if no_experience else "опыт требуется"
    return None


def _salary_state(job: ReviewJobInput) -> str:
    if job.salary_min is not None or job.salary_max is not None:
        return "numeric"
    text = _normalized(job.salary_text, limit=500)
    if text is None or _MISSING_SALARY.fullmatch(text):
        return "missing"
    return "textual"


def _location_features(job: ReviewJobInput) -> tuple[list[str], list[str]]:
    values = _unique(
        [
            canonicalize_location(job.location),
            *(canonicalize_location(item) for item in job.cities),
        ]
    )
    primary = canonicalize_location(job.location) or (values[0] if values else None)
    cities: list[str] = []
    areas: list[str] = []
    for value in values:
        if _REMOTE_LOCATION.search(value):
            continue
        if value == primary or value in _CITY_LABELS:
            cities.append(value)
        else:
            areas.append(value)
    return cities[:4], areas[:4]


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
    cities, areas = _location_features(job)
    experience = _canonical_experience(job.required_experience, job.no_experience)
    return {
        "category": categories,
        "title": title_terms,
        "city": cities,
        "area": areas,
        "schedule": _unique([_canonical_schedule(job.schedule)]),
        "workplace": _unique([_normalized(job.workplace_type)]),
        "experience": _unique([experience]),
        "company": _unique([_normalized(job.company)]),
        "salary": [_salary_state(job)],
    }


def _feature_label(dimension: str, value: str) -> str:
    if dimension == "city":
        value = _CITY_LABELS.get(value, value)
    elif dimension == "schedule":
        value = _SCHEDULE_LABELS.get(value, value)
    elif dimension == "salary":
        value = _SALARY_LABELS.get(value, value)
    return f"{_DIMENSION_LABELS.get(dimension, dimension)}: {value}"


def _normalized_feature_value(dimension: str, value: Any) -> str | None:
    raw = str(value)
    if dimension == "city":
        return canonicalize_location(raw) or None
    if dimension == "schedule":
        return _canonical_schedule(raw)
    if dimension == "experience":
        return _canonical_experience(raw)
    return _normalized(raw)


def _event_features(event: ReviewFeedbackEvent) -> dict[str, list[str]]:
    raw_features = event.feature_snapshot.get("features", {})
    if not isinstance(raw_features, dict):
        return {}
    result: dict[str, list[str]] = {}
    for raw_dimension, raw_values in raw_features.items():
        dimension = str(raw_dimension)
        if not isinstance(raw_values, list):
            continue
        values = _unique(
            [_normalized_feature_value(dimension, raw_value) for raw_value in raw_values]
        )
        if event.feature_schema_version == LEGACY_FEATURE_SCHEMA_VERSION and dimension == "city":
            primary = values[0] if values else None
            result["city"] = [
                value for value in values if value == primary or value in _CITY_LABELS
            ][:4]
            result["area"] = [
                value
                for value in values
                if value != primary
                and value not in _CITY_LABELS
                and not _REMOTE_LOCATION.search(value)
            ][:4]
            continue
        result[dimension] = values[:8]
    return result


def _event_resume_category(
    event: ReviewFeedbackEvent,
    resume_categories_by_sha: Mapping[str, str],
) -> str | None:
    raw_context = event.feature_snapshot.get("context", {})
    if isinstance(raw_context, dict):
        category = _normalized(str(raw_context.get("resume_category") or ""))
        if category:
            return category
    if event.resume_sha256:
        return resume_categories_by_sha.get(event.resume_sha256)
    return None


def _labels_needed(stat: _DimensionStat) -> int:
    return max(
        0,
        MINIMUM_LABELS - stat.support,
        max(0, MINIMUM_PER_OUTCOME - stat.approved) + max(0, MINIMUM_PER_OUTCOME - stat.rejected),
    )


def _feature_delta(stat: _FeatureStat, dimension: _DimensionStat) -> float:
    acceptance = (stat.approved + 1) / (stat.support + 2)
    dimension_baseline = (dimension.approved + 1) / (dimension.support + 2)
    return (acceptance - dimension_baseline) * min(1.0, stat.support / 5)


def _summarize_events(
    events: list[ReviewFeedbackEvent],
    *,
    influence_enabled: bool,
    ignored_dimensions: Iterable[str] = (),
    resume_categories_by_sha: Mapping[str, str] | None = None,
    resume_category: str | None = None,
    restrict_resume_category: bool = False,
) -> ReviewLearningSummary:
    supported_events = [
        event
        for event in events
        if event.learning_eligible and event.feature_schema_version in _SCHEMA_DIMENSIONS
    ]
    approved = sum(event.outcome == ReviewOutcome.APPROVED for event in supported_events)
    rejected = sum(event.outcome == ReviewOutcome.REJECTED for event in supported_events)
    excluded = len(events) - len(supported_events)
    ignored = set(ignored_dimensions)
    target_resume_category = _normalized(resume_category)
    category_by_sha = resume_categories_by_sha or {}
    mutable_stats: dict[str, dict[str, Any]] = {}
    mutable_dimensions: dict[str, dict[str, int]] = {}
    for event in supported_events:
        features = _event_features(event)
        raw_dimensions = event.feature_snapshot.get("learning_dimensions", [])
        if not isinstance(raw_dimensions, list):
            continue
        compatible = _SCHEMA_DIMENSIONS[event.feature_schema_version]
        dimensions = {str(item) for item in raw_dimensions} & compatible
        if (
            event.feature_schema_version == LEGACY_FEATURE_SCHEMA_VERSION
            and "city" in dimensions
            and features.get("area")
        ):
            dimensions.add("area")
        dimensions -= ignored
        if restrict_resume_category and (
            target_resume_category is None
            or _event_resume_category(event, category_by_sha) != target_resume_category
        ):
            dimensions -= _RESUME_DEPENDENT_DIMENSIONS
        for dimension in dimensions:
            dimension_entry = mutable_dimensions.setdefault(
                dimension, {"approved": 0, "rejected": 0}
            )
            dimension_entry[event.outcome.value] += 1
            for value in features.get(dimension, []):
                key = f"{dimension}\x00{value}"
                entry = mutable_stats.setdefault(
                    key,
                    {"dimension": dimension, "value": value, "approved": 0, "rejected": 0},
                )
                entry[event.outcome.value] += 1
    stats = {key: _FeatureStat(**value) for key, value in mutable_stats.items()}
    dimension_stats = {
        dimension: _DimensionStat(**values) for dimension, values in mutable_dimensions.items()
    }
    ready = any(stat.ready for stat in dimension_stats.values())
    proposals: list[LearningProposal] = []
    for stat in stats.values():
        dimension_stat = dimension_stats.get(stat.dimension)
        if dimension_stat is None or not dimension_stat.ready or stat.support < 3:
            continue
        delta = _feature_delta(stat, dimension_stat)
        if abs(delta) < 0.1:
            continue
        direction = "чаще принимаете" if delta > 0 else "чаще отклоняете"
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
    labels_needed = (
        0
        if ready
        else min(
            (_labels_needed(stat) for stat in dimension_stats.values()),
            default=MINIMUM_LABELS,
        )
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
        _dimension_stats=dimension_stats,
    )


def _score(summary: ReviewLearningSummary, job: ReviewJobInput) -> LearnedReviewScore | None:
    if not summary.ready or not summary.influence_enabled:
        return None
    total = summary.approved + summary.rejected
    baseline = (summary.approved + 1) / (total + 2)
    dimension_factors: list[tuple[float, _FeatureStat]] = []
    for dimension, values in _feature_snapshot(job).items():
        dimension_stat = summary._dimension_stats.get(dimension)
        if dimension_stat is None or not dimension_stat.ready:
            continue
        options: list[tuple[float, _FeatureStat]] = []
        for value in values:
            stat = summary._feature_stats.get(f"{dimension}\x00{value}")
            if stat is None or stat.support < 2:
                continue
            delta = _feature_delta(stat, dimension_stat)
            if abs(delta) < 0.05:
                continue
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
        resume = await session.get(Resume, application.resume_id)
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
            or not await evaluation_is_current(session, evaluation, job)
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
            "context": {
                "resume_category": resume.category if resume is not None else None,
            },
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
        resume_category: str | None = None,
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
        resumes = list(
            (await session.scalars(select(Resume).where(Resume.profile_id == profile_id))).all()
        )
        resume_categories_by_sha = {
            resume.sha256: category
            for resume in resumes
            if (category := _normalized(resume.category)) is not None
        }
        active_resume_categories = {
            category
            for resume in resumes
            if resume.active and (category := _normalized(resume.category)) is not None
        }
        if resume_category is not None:
            target_resume_category = _normalized(resume_category)
        elif len(active_resume_categories) == 1:
            target_resume_category = next(iter(active_resume_categories))
        else:
            target_resume_category = None
        return _summarize_events(
            events,
            influence_enabled=setting.influence_enabled if setting is not None else True,
            ignored_dimensions=ignored_dimensions,
            resume_categories_by_sha=resume_categories_by_sha,
            resume_category=target_resume_category,
            restrict_resume_category=bool(resumes),
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
