from datetime import UTC, datetime
from uuid import uuid4

from app.learning.features import (
    MIN_VOCAB_SUPPORT,
    FeatureSpec,
    build_feature_spec,
)
from app.models.entities import ReviewFeedbackEvent
from app.models.enums import ReviewOutcome


def _event(**snapshot_features: list[str]) -> ReviewFeedbackEvent:
    return ReviewFeedbackEvent(
        profile_id=uuid4(),
        application_id=uuid4(),
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        outcome=ReviewOutcome.APPROVED,
        actor="test",
        learning_eligible=True,
        feature_schema_version="review-v2",
        feature_snapshot={
            "features": snapshot_features,
            "learning_dimensions": list(snapshot_features),
        },
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def test_feature_spec_keeps_only_values_over_the_support_floor() -> None:
    events = [_event(category=["warehouses"], title=["picker"]) for _ in range(MIN_VOCAB_SUPPORT)]
    events.append(_event(category=["rare"], title=["oddball"]))

    spec = build_feature_spec(events)

    assert "warehouses" in spec.categorical["category"]
    assert "rare" not in spec.categorical["category"]
    assert spec.version == "features-v3"


def test_feature_names_are_ordered_and_start_with_intercept() -> None:
    events = [_event(category=["warehouses"]) for _ in range(MIN_VOCAB_SUPPORT)]
    spec = build_feature_spec(events)

    names = spec.feature_names()

    assert names[0] == "__intercept__"
    assert "category:warehouses" in names
    assert "obs:category" in names
    assert "overall_fit" in names
    assert names == tuple(dict.fromkeys(names))  # no duplicates, stable order


def test_feature_spec_round_trips_through_dict() -> None:
    events = [_event(category=["warehouses"]) for _ in range(MIN_VOCAB_SUPPORT)]
    spec = build_feature_spec(events)

    assert FeatureSpec.from_dict(spec.to_dict()).feature_names() == spec.feature_names()
