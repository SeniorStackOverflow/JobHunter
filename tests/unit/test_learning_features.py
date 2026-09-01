from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.learning.features import (
    MIN_VOCAB_SUPPORT,
    ExtractedFeatures,
    FeatureSpec,
    age_bucket_for,
    build_feature_spec,
    build_matrix,
    build_snapshot_extras,
    extract_from_event,
    present_values,
    vectorize,
)
from app.models.entities import JobPreference, MatchEvaluation, ReviewFeedbackEvent, SourceJob
from app.models.enums import MatchDecision, ReviewOutcome, ReviewReason


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


def _rejected_event(dimensions: list[str], reason: ReviewReason) -> ReviewFeedbackEvent:
    return ReviewFeedbackEvent(
        profile_id=uuid4(),
        application_id=uuid4(),
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        outcome=ReviewOutcome.REJECTED,
        reason_code=reason,
        actor="test",
        learning_eligible=True,
        feature_schema_version="review-v2",
        feature_snapshot={
            "features": {"category": ["sales"], "salary": ["missing"]},
            "learning_dimensions": dimensions,
            "numeric": {"overall_fit": 40.0},
        },
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def test_event_extraction_reads_label_and_active_dimensions() -> None:
    event = _rejected_event(["salary"], ReviewReason.SALARY)

    features, label = extract_from_event(event)

    assert label == 0.0
    assert features.active_dimensions == frozenset({"salary"})
    assert features.numeric["overall_fit"] == 0.4  # normalised
    assert features.numeric["salary_missing"] == 1.0  # no salary key stored -> stays neutral


def test_missing_numeric_block_is_flagged() -> None:
    event = ReviewFeedbackEvent(
        profile_id=uuid4(),
        application_id=uuid4(),
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        outcome=ReviewOutcome.APPROVED,
        actor="test",
        learning_eligible=True,
        feature_schema_version="review-v2",
        feature_snapshot={
            "features": {"category": ["warehouses"]},
            "learning_dimensions": ["category"],
        },
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )

    features, _ = extract_from_event(event)

    assert features.numeric["llm_scores_missing"] == 1.0
    assert features.numeric["overall_fit"] == 0.5
    assert features.source_key == "__other__"
    assert features.age_bucket == "unknown"


def test_snapshot_extras_normalise_scores_and_salary_gap() -> None:
    job = SourceJob(
        source_id=uuid4(),
        external_job_id="x",
        canonical_url="https://e/j",
        title="Picker",
        content_hash="a",
        matching_content_hash="b",
        source_fingerprint="c",
        salary_min=Decimal("8000"),
    )
    evaluation = MatchEvaluation(
        profile_id=uuid4(),
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        resume_fit=60,
        preference_fit=80,
        overall_fit=72,
        requirements_met=[],
        missing_requirements=["x"],
        risks=[],
        scam_indicators=[],
        explanation="",
        decision=MatchDecision.PREPARE_FOR_REVIEW,
        model="m",
        prompt_rules_version="v",
    )
    preference = JobPreference(profile_id=uuid4(), minimum_salary=Decimal("10000"))

    extras = build_snapshot_extras(job, evaluation, preference)

    assert extras["numeric"]["overall_fit"] == 0.72
    assert extras["numeric"]["llm_prepare"] == 1.0
    assert extras["numeric"]["n_missing_requirements"] == 0.2  # 1 / 5
    assert extras["numeric"]["salary_gap"] == -0.2  # (8000 - 10000) / 10000
    assert extras["numeric"]["salary_missing"] == 0.0


def _approved(dimensions: list[str], **features: list[str]) -> ReviewFeedbackEvent:
    return ReviewFeedbackEvent(
        profile_id=uuid4(),
        application_id=uuid4(),
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        outcome=ReviewOutcome.APPROVED,
        actor="test",
        learning_eligible=True,
        feature_schema_version="review-v2",
        feature_snapshot={"features": features, "learning_dimensions": dimensions},
        created_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def test_vectorize_applies_the_causal_mask() -> None:
    spec = build_feature_spec(
        [
            _approved(["category", "title"], category=["warehouses"], title=["picker"])
            for _ in range(MIN_VOCAB_SUPPORT)
        ]
        + [_approved(["category"], category=["sales"]) for _ in range(MIN_VOCAB_SUPPORT)]
    )
    # the salary-rejection event carries category=["sales"], which is in the vocab
    assert "sales" in spec.categorical["category"]
    features, _ = extract_from_event(_rejected_event(["salary"], ReviewReason.SALARY))
    # rejected-for-salary event: category "sales" present in snapshot but not active
    names = spec.feature_names()[1:]
    row = vectorize(spec, features)

    assert row[names.index("obs:category")] == 0.0  # category dimension masked out
    # the one-hot must be zeroed by the mask, not merely absent from the vocab
    assert row[names.index("category:sales")] == 0.0


def test_present_values_skips_masked_dimensions() -> None:
    spec = build_feature_spec(
        [_approved(["category"], category=["warehouses"]) for _ in range(MIN_VOCAB_SUPPORT)]
    )
    common = {
        "categorical": {"category": ["warehouses"]},
        "numeric": {},
        "source_key": "__other__",
        "age_bucket": "unknown",
    }
    masked = ExtractedFeatures(active_dimensions=frozenset(), **common)
    active = ExtractedFeatures(active_dimensions=frozenset({"category"}), **common)

    assert present_values(spec, masked) == []  # category not active -> not counted
    assert present_values(spec, active) == ["category:warehouses"]


def test_build_matrix_frequency_ignores_masked_categoricals() -> None:
    active_events = [
        _approved(["category"], category=["warehouses"]) for _ in range(MIN_VOCAB_SUPPORT)
    ]
    # a salary-only rejection whose snapshot still carries category=warehouses
    masked = ReviewFeedbackEvent(
        profile_id=uuid4(),
        application_id=uuid4(),
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        outcome=ReviewOutcome.REJECTED,
        reason_code=ReviewReason.SALARY,
        actor="test",
        learning_eligible=True,
        feature_schema_version="review-v2",
        feature_snapshot={
            "features": {"category": ["warehouses"]},
            "learning_dimensions": ["salary"],
        },
        created_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    spec = build_feature_spec([*active_events, masked])

    _x, _y, _w, freq = build_matrix(
        [*active_events, masked], spec, now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    # counted for the active events only; the masked row's zero column is not
    assert freq["category:warehouses"] == MIN_VOCAB_SUPPORT


def test_matrix_weights_decay_with_age() -> None:
    old = _approved(["category"], category=["warehouses"])
    old.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    recent = _approved(["category"], category=["warehouses"])
    recent.created_at = datetime(2026, 8, 20, tzinfo=UTC)
    spec = build_feature_spec(
        [old, recent]
        + [_approved(["category"], category=["warehouses"]) for _ in range(MIN_VOCAB_SUPPORT)]
    )

    x, _y, w, freq = build_matrix([old, recent], spec, now=datetime(2026, 8, 21, tzinfo=UTC))

    assert x.shape[0] == 2
    assert x[:, 0].tolist() == [1.0, 1.0]  # intercept column
    assert w[0] < w[1]
    assert freq["category:warehouses"] == 2


def test_build_matrix_tolerates_naive_created_at() -> None:
    naive_events = []
    for _ in range(3):
        e = _approved(["category"], category=["warehouses"])
        e.created_at = datetime(2026, 3, 1)  # naive, as SQLite returns
        naive_events.append(e)
    spec = build_feature_spec(naive_events)

    x, _y, w, _freq = build_matrix(naive_events, spec, now=datetime(2026, 8, 1, tzinfo=UTC))

    assert x.shape[0] == 3
    assert (w > 0).all()


def test_age_bucket_for_tolerates_naive_published_at() -> None:
    job = SourceJob(
        source_id=uuid4(),
        external_job_id="x",
        canonical_url="https://e/j",
        title="T",
        content_hash="a",
        matching_content_hash="b",
        source_fingerprint="c",
        published_at=datetime(2026, 8, 20),  # naive
    )
    assert age_bucket_for(job) in {"0-3", "4-7", "8-30", "31+"}


def test_present_values_ignores_out_of_vocab() -> None:
    spec = build_feature_spec(
        [_approved(["category"], category=["warehouses"]) for _ in range(MIN_VOCAB_SUPPORT)]
    )
    features, _ = extract_from_event(_approved(["category"], category=["warehouses", "unlisted"]))

    assert present_values(spec, features) == ["category:warehouses"]
