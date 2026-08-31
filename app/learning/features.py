from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.learning.service import _ALL_DIMENSIONS, _DIMENSION_LABELS, _feature_label
from app.models.entities import ReviewFeedbackEvent

FEATURE_SPEC_VERSION = "features-v3"
HALF_LIFE_DAYS = 120
MIN_FEATURE_SUPPORT = 5
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
