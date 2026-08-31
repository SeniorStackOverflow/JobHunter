import numpy as np

from app.learning.model import fit_l2_logistic


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
    assert abs(strong[1]) < 0.05


def test_sample_weight_moves_the_intercept() -> None:
    x = _design([(0.0,)] * 10)
    y = np.array([1.0] * 7 + [0.0] * 3)
    heavy_positive = np.array([5.0] * 7 + [1.0] * 3)

    beta = fit_l2_logistic(x, y, heavy_positive, l2=0.0)

    # weighted positive rate 35/38 -> logit ~ 2.45
    assert 2.0 < beta[0] < 3.0
