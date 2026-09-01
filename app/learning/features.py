from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
from numpy.typing import NDArray

from app.learning.service import (
    _ALL_DIMENSIONS,
    _DIMENSION_LABELS,
    _feature_label,
    _feature_snapshot,
    review_job_input,
)
from app.models.entities import JobPreference, MatchEvaluation, ReviewFeedbackEvent, SourceJob
from app.models.enums import MatchDecision

FEATURE_SPEC_VERSION = "features-v3"
HALF_LIFE_DAYS = 120
MIN_VOCAB_SUPPORT = 3
TITLE_VOCAB_CAP = 40
OTHER_VOCAB_CAP = 24
MODEL_MIN_LABELS = 40
MODEL_MIN_PER_OUTCOME = 8
MODEL_ELIGIBLE_SCHEMAS = frozenset({"review-v2"})

NUMERIC_NAMES: tuple[str, ...] = (
    "overall_fit",
    "resume_fit",
    "preference_fit",
    "llm_scores_missing",
    "n_missing_requirements",
    "n_risks",
    "salary_gap",
    "salary_missing",
    "llm_auto_apply",
    "llm_prepare",
    "llm_skip",
    "llm_block",
    "llm_decision_missing",
)
AGE_BUCKETS: tuple[str, ...] = ("0-3", "4-7", "8-30", "31+", "unknown")
_VOCAB_CAP = {dimension: OTHER_VOCAB_CAP for dimension in _ALL_DIMENSIONS}
_VOCAB_CAP["title"] = TITLE_VOCAB_CAP


@dataclass(frozen=True)
class FeatureSpec:
    version: str
    categorical: dict[str, tuple[str, ...]]
    source_keys: tuple[str, ...]

    def feature_names(self) -> tuple[str, ...]:
        names: list[str] = ["__intercept__"]
        for dimension in _ALL_DIMENSIONS:
            for value in self.categorical.get(dimension, ()):  # ordered
                names.append(f"{dimension}:{value}")
            names.append(f"obs:{dimension}")
        names.extend(NUMERIC_NAMES)
        for key in self.source_keys:
            names.append(f"source:{key}")
        names.append("source:__other__")
        for bucket in AGE_BUCKETS:
            names.append(f"age:{bucket}")
        return tuple(names)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "categorical": {k: list(v) for k, v in self.categorical.items()},
            "source_keys": list(self.source_keys),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureSpec:
        return cls(
            version=str(data["version"]),
            categorical={k: tuple(v) for k, v in data["categorical"].items()},
            source_keys=tuple(data["source_keys"]),
        )


@dataclass(frozen=True)
class ExtractedFeatures:
    categorical: dict[str, list[str]]
    numeric: dict[str, float]
    source_key: str
    age_bucket: str
    active_dimensions: frozenset[str]


def build_feature_spec(events: Sequence[ReviewFeedbackEvent]) -> FeatureSpec:
    counts: dict[str, Counter[str]] = {d: Counter() for d in _ALL_DIMENSIONS}
    source_counts: Counter[str] = Counter()
    for event in events:
        if event.feature_schema_version not in MODEL_ELIGIBLE_SCHEMAS:
            continue
        snapshot = event.feature_snapshot or {}
        raw_features = snapshot.get("features", {})
        if isinstance(raw_features, dict):
            for dimension in _ALL_DIMENSIONS:
                for value in raw_features.get(dimension, []) or []:
                    if isinstance(value, str) and value:
                        counts[dimension][value] += 1
        context = snapshot.get("context", {})
        if isinstance(context, dict):
            key = context.get("source_key")
            if isinstance(key, str) and key:
                source_counts[key] += 1
    categorical = {
        dimension: tuple(
            value
            for value, count in counter.most_common(_VOCAB_CAP[dimension])
            if count >= MIN_VOCAB_SUPPORT
        )
        for dimension, counter in counts.items()
    }
    source_keys = tuple(
        key
        for key, count in source_counts.most_common(OTHER_VOCAB_CAP)
        if count >= MIN_VOCAB_SUPPORT
    )
    return FeatureSpec(
        version=FEATURE_SPEC_VERSION, categorical=categorical, source_keys=source_keys
    )


def dimension_label(dimension: str) -> str:
    return _DIMENSION_LABELS.get(dimension, dimension)


def contribution_labels(spec: FeatureSpec) -> dict[str, str]:
    labels: dict[str, str] = {}
    for dimension, values in spec.categorical.items():
        for value in values:
            labels[f"{dimension}:{value}"] = _feature_label(dimension, value)
    return labels


_NEUTRAL_NUMERIC: dict[str, float] = {
    "overall_fit": 0.5,
    "resume_fit": 0.5,
    "preference_fit": 0.5,
    "llm_scores_missing": 1.0,
    "n_missing_requirements": 0.0,
    "n_risks": 0.0,
    "salary_gap": 0.0,
    "salary_missing": 1.0,
    "llm_auto_apply": 0.0,
    "llm_prepare": 0.0,
    "llm_skip": 0.0,
    "llm_block": 0.0,
    "llm_decision_missing": 1.0,
}
_LLM_DECISION_FLAG = {
    MatchDecision.AUTO_APPLY: "llm_auto_apply",
    MatchDecision.PREPARE_FOR_REVIEW: "llm_prepare",
    MatchDecision.SKIP: "llm_skip",
    MatchDecision.BLOCK: "llm_block",
}


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_aware(value: datetime) -> datetime:
    """Coerce a possibly-naive datetime (SQLite round-trips tz-aware columns as
    naive) to UTC-aware so arithmetic against ``datetime.now(UTC)`` is safe."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def age_bucket_for(job: SourceJob) -> str:
    published = job.published_at
    if published is None:
        return "unknown"
    days = (datetime.now(UTC) - _as_aware(published)).days
    if days <= 3:
        return "0-3"
    if days <= 7:
        return "4-7"
    if days <= 30:
        return "8-30"
    return "31+"


def numeric_from_evaluation(
    evaluation: MatchEvaluation | None,
    job: SourceJob,
    preference: JobPreference | None,
) -> dict[str, float]:
    numeric = dict(_NEUTRAL_NUMERIC)
    if evaluation is not None:
        numeric["overall_fit"] = _clip(evaluation.overall_fit / 100.0, 0.0, 1.0)
        numeric["resume_fit"] = _clip(evaluation.resume_fit / 100.0, 0.0, 1.0)
        numeric["preference_fit"] = _clip(evaluation.preference_fit / 100.0, 0.0, 1.0)
        numeric["llm_scores_missing"] = 0.0
        numeric["n_missing_requirements"] = _clip(
            len(evaluation.missing_requirements or []) / 5.0, 0.0, 1.0
        )
        numeric["n_risks"] = _clip(len(evaluation.risks or []) / 5.0, 0.0, 1.0)
        for flag in ("llm_auto_apply", "llm_prepare", "llm_skip", "llm_block"):
            numeric[flag] = 0.0
        flag_name = _LLM_DECISION_FLAG.get(evaluation.decision)
        if flag_name is not None:
            numeric[flag_name] = 1.0
            numeric["llm_decision_missing"] = 0.0
    salary_value = job.salary_min if job.salary_min is not None else job.salary_max
    minimum = preference.minimum_salary if preference is not None else None
    if salary_value is not None and minimum is not None and minimum > 0:
        gap = (Decimal(salary_value) - Decimal(minimum)) / Decimal(minimum)
        numeric["salary_gap"] = _clip(float(gap), -1.0, 3.0)
        numeric["salary_missing"] = 0.0
    elif salary_value is not None:
        numeric["salary_missing"] = 0.0
    return numeric


def build_snapshot_extras(
    job: SourceJob,
    evaluation: MatchEvaluation | None,
    preference: JobPreference | None,
) -> dict[str, Any]:
    return {
        "numeric": numeric_from_evaluation(evaluation, job, preference),
        "context": {"source_key": _source_key(job), "age_bucket": age_bucket_for(job)},
    }


def _source_key(job: SourceJob) -> str:
    raw = (job.raw_metadata or {}).get("adapter_type")
    return str(raw) if isinstance(raw, str) and raw else "__other__"


def extract_from_event(event: ReviewFeedbackEvent) -> tuple[ExtractedFeatures, float]:
    snapshot = event.feature_snapshot or {}
    raw_features = snapshot.get("features", {})
    categorical = {
        dimension: [
            value
            for value in (raw_features.get(dimension, []) or [])
            if isinstance(value, str) and value
        ]
        for dimension in _ALL_DIMENSIONS
    }
    numeric = dict(_NEUTRAL_NUMERIC)
    stored_numeric = snapshot.get("numeric", {})
    if isinstance(stored_numeric, dict):
        raw_overall = stored_numeric.get("overall_fit")
        legacy_scale = isinstance(raw_overall, (int, float)) and float(raw_overall) > 1.0
        for name in NUMERIC_NAMES:
            value = stored_numeric.get(name)
            if value is None:
                continue
            number = float(value)
            if legacy_scale and name in ("overall_fit", "resume_fit", "preference_fit"):
                number = _clip(number / 100.0, 0.0, 1.0)
            numeric[name] = number
        # a stored raw score with no explicit flag means "scores are present"
        if raw_overall is not None and "llm_scores_missing" not in stored_numeric:
            numeric["llm_scores_missing"] = 0.0
    context = snapshot.get("context", {})
    source_key = "__other__"
    age_bucket = "unknown"
    if isinstance(context, dict):
        if isinstance(context.get("source_key"), str) and context["source_key"]:
            source_key = context["source_key"]
        if context.get("age_bucket") in AGE_BUCKETS:
            age_bucket = str(context["age_bucket"])
    raw_dimensions = snapshot.get("learning_dimensions", [])
    active = (
        frozenset(str(item) for item in raw_dimensions if str(item) in _ALL_DIMENSIONS)
        if isinstance(raw_dimensions, list)
        else frozenset()
    )
    label = 1.0 if event.outcome.value == "approved" else 0.0
    return (
        ExtractedFeatures(
            categorical=categorical,
            numeric=numeric,
            source_key=source_key,
            age_bucket=age_bucket,
            active_dimensions=active,
        ),
        label,
    )


def extract_live(
    job: SourceJob,
    evaluation: MatchEvaluation,
    preference: JobPreference,
    *,
    source_key: str | None = None,
) -> ExtractedFeatures:
    snapshot = _feature_snapshot(review_job_input(job))
    categorical = {dimension: list(snapshot.get(dimension, [])) for dimension in _ALL_DIMENSIONS}
    return ExtractedFeatures(
        categorical=categorical,
        numeric=numeric_from_evaluation(evaluation, job, preference),
        source_key=source_key or _source_key(job),
        age_bucket=age_bucket_for(job),
        active_dimensions=frozenset(_ALL_DIMENSIONS),
    )


def present_values(spec: FeatureSpec, features: ExtractedFeatures) -> list[str]:
    found: list[str] = []
    for dimension in _ALL_DIMENSIONS:
        if dimension not in features.active_dimensions:
            continue
        vocab = set(spec.categorical.get(dimension, ()))
        for value in features.categorical.get(dimension, []):
            key = f"{dimension}:{value}"
            if value in vocab and key not in found:
                found.append(key)
    return found


def vectorize(spec: FeatureSpec, features: ExtractedFeatures) -> NDArray[np.float64]:
    names = spec.feature_names()[1:]
    index = {name: i for i, name in enumerate(names)}
    row = np.zeros(len(names), dtype=np.float64)
    for dimension in _ALL_DIMENSIONS:
        active = dimension in features.active_dimensions
        row[index[f"obs:{dimension}"]] = 1.0 if active else 0.0
        if not active:
            continue
        vocab = set(spec.categorical.get(dimension, ()))
        for value in features.categorical.get(dimension, []):
            if value in vocab:
                row[index[f"{dimension}:{value}"]] = 1.0
    for name in NUMERIC_NAMES:
        row[index[name]] = float(features.numeric.get(name, _NEUTRAL_NUMERIC[name]))
    source_name = f"source:{features.source_key}"
    row[index[source_name if source_name in index else "source:__other__"]] = 1.0
    bucket = features.age_bucket if features.age_bucket in AGE_BUCKETS else "unknown"
    row[index[f"age:{bucket}"]] = 1.0
    return row


def build_matrix(
    events: Sequence[ReviewFeedbackEvent],
    spec: FeatureSpec,
    *,
    now: datetime | None = None,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64], dict[str, int]]:
    moment = _as_aware(now) if now is not None else datetime.now(UTC)
    usable = sorted(
        (
            event
            for event in events
            if event.learning_eligible and event.feature_schema_version in MODEL_ELIGIBLE_SCHEMAS
        ),
        key=lambda e: _as_aware(e.created_at),
    )
    rows: list[NDArray[np.float64]] = []
    labels: list[float] = []
    weights: list[float] = []
    frequencies: Counter[str] = Counter()
    for event in usable:
        features, label = extract_from_event(event)
        rows.append(vectorize(spec, features))
        labels.append(label)
        age_days = max(0, (moment - _as_aware(event.created_at)).days)
        weights.append(0.5 ** (age_days / HALF_LIFE_DAYS))
        for key in present_values(spec, features):
            frequencies[key] += 1
    if not rows:
        width = len(spec.feature_names())
        empty = np.empty((0, width), dtype=np.float64)
        return empty, np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64), {}
    body = np.vstack(rows)
    x = np.column_stack([np.ones(len(rows), dtype=np.float64), body])
    return (
        x,
        np.array(labels, dtype=np.float64),
        np.array(weights, dtype=np.float64),
        dict(frequencies),
    )
