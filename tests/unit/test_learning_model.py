import numpy as np

from app.learning.model import IsotonicCalibration, fit_l2_logistic, pava_isotonic


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
