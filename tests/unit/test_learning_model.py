import numpy as np

from app.learning.model import (
    CvResult,
    IsotonicCalibration,
    expected_calibration_error,
    fit_l2_logistic,
    pava_isotonic,
    time_series_cv,
    weighted_auc,
    weighted_logloss,
)


def _design(rows: list[tuple[float, ...]] | list[tuple[float, float]]) -> np.ndarray:
    body = np.array(rows, dtype=np.float64)
    return np.column_stack([np.ones(len(body)), body])


def test_logistic_recovers_a_clear_separation() -> None:
    x = _design([(-2.0, 0.0), (-1.0, 0.0), (1.0, 0.0), (2.0, 0.0)] * 20)
    y = np.array([0.0, 0.0, 1.0, 1.0] * 20)
    w = np.ones(len(y))

    beta = fit_l2_logistic(x, y, w, l2=0.01)

    assert beta[1] > 2.0  # strong positive slope on the separating feature
    assert abs(beta[0]) < 0.5  # near-zero intercept for a balanced set


def test_l2_shrinks_slope_toward_zero() -> None:
    x = _design([(-2.0,), (-1.0,), (1.0,), (2.0,)] * 20)
    y = np.array([0.0, 0.0, 1.0, 1.0] * 20)
    w = np.ones(len(y))

    weak = fit_l2_logistic(x, y, w, l2=0.01)
    strong = fit_l2_logistic(x, y, w, l2=1000.0)

    assert abs(strong[1]) < abs(weak[1])
    assert abs(strong[1]) < 0.1


def test_sample_weight_moves_the_intercept() -> None:
    x = _design([(0.0,)] * 10)
    y = np.array([1.0] * 7 + [0.0] * 3)
    heavy_positive = np.array([5.0] * 7 + [1.0] * 3)

    beta = fit_l2_logistic(x, y, heavy_positive, l2=0.0)

    # weighted positive rate 35/38 -> logit ~ 2.45
    assert 2.0 < beta[0] < 3.0


def test_isotonic_output_is_monotone_and_pools_violators() -> None:
    raw = np.array([0.1, 0.2, 0.3, 0.4, 0.5], dtype=np.float64)
    y = np.array([0.0, 1.0, 0.0, 1.0, 1.0], dtype=np.float64)  # non-monotone
    w = np.ones(5)

    cal = pava_isotonic(raw, y, w)
    predictions = [cal.predict(v) for v in raw]

    assert predictions == sorted(predictions)
    assert all(0.0 <= p <= 1.0 for p in predictions)


def test_isotonic_recovers_a_clean_ramp() -> None:
    raw = np.concatenate([np.full(50, 0.2), np.full(50, 0.8)])
    y = np.concatenate([np.zeros(50), np.ones(50)])
    w = np.ones(100)

    cal = pava_isotonic(raw, y, w)

    assert cal.predict(0.2) < 0.1
    assert cal.predict(0.8) > 0.9


def test_small_block_returns_a_maximally_wide_interval() -> None:
    raw = np.array([0.5, 0.5, 0.5], dtype=np.float64)
    y = np.array([1.0, 1.0, 0.0], dtype=np.float64)
    w = np.ones(3)

    cal = pava_isotonic(raw, y, w)

    assert cal.interval(0.5) == (0.0, 1.0)


def test_isotonic_round_trips_through_dict() -> None:
    raw = np.linspace(0.0, 1.0, 40)
    y = (raw > 0.5).astype(np.float64)
    cal = pava_isotonic(raw, y, np.ones(40))

    restored = IsotonicCalibration.from_dict(cal.to_dict())

    assert restored.predict(0.7) == cal.predict(0.7)
    assert restored.interval(0.7) == cal.interval(0.7)


def test_auc_is_one_for_a_perfect_ranker_and_half_for_noise() -> None:
    y = np.array([0.0, 0.0, 1.0, 1.0])
    w = np.ones(4)
    assert weighted_auc(y, np.array([0.1, 0.2, 0.8, 0.9]), w) == 1.0
    assert weighted_auc(y, np.array([0.5, 0.5, 0.5, 0.5]), w) == 0.5


def test_logloss_rewards_confident_correct_predictions() -> None:
    y = np.array([1.0, 0.0])
    w = np.ones(2)
    good = weighted_logloss(y, np.array([0.95, 0.05]), w)
    bad = weighted_logloss(y, np.array([0.55, 0.45]), w)
    assert good < bad


def test_ece_is_zero_for_perfectly_calibrated_predictions() -> None:
    y = np.concatenate([np.zeros(50), np.ones(50)])
    p = np.concatenate([np.zeros(50), np.ones(50)])
    assert expected_calibration_error(y, p, np.ones(100)) == 0.0


def test_ece_flags_overconfidence() -> None:
    y = np.concatenate([np.zeros(50), np.ones(50)])
    p = np.full(100, 0.99)  # always confident, only right half the time
    assert expected_calibration_error(y, p, np.ones(100)) > 0.4


def _ramped_design(n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    feature = rng.normal(size=n)
    logit = 1.5 * feature
    y = (rng.uniform(size=n) < 1.0 / (1.0 + np.exp(-logit))).astype(np.float64)
    x = np.column_stack([np.ones(n), feature])
    return x, y, np.ones(n)


def test_cv_selects_a_grid_value_and_reports_reasonable_auc() -> None:
    x, y, w = _ramped_design(200)

    result = time_series_cv(x, y, w)

    assert isinstance(result, CvResult)
    assert result.best_l2 in (0.03, 0.1, 0.3, 1.0, 3.0)
    assert result.cv_auc > 0.65
    assert result.oof_raw.size > 0


def test_cv_degrades_gracefully_on_tiny_input() -> None:
    x, y, w = _ramped_design(3)

    result = time_series_cv(x, y, w)

    assert result.best_l2 == 0.3
    assert result.cv_auc == 0.5
