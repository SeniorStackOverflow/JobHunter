from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

SHADOW_APPROVE_P = 0.90
SHADOW_REJECT_P = 0.12
CI_MAX_WIDTH = 0.15
MIN_FEATURE_SUPPORT = 5
L2_GRID: tuple[float, ...] = (0.03, 0.1, 0.3, 1.0, 3.0)
CV_FOLDS = 4


def _sigmoid(eta: NDArray[np.float64]) -> NDArray[np.float64]:
    return 1.0 / (1.0 + np.exp(-np.clip(eta, -30.0, 30.0)))


def fit_l2_logistic(
    x: NDArray[np.float64],
    y: NDArray[np.float64],
    sample_weight: NDArray[np.float64],
    l2: float,
    *,
    max_iter: int = 100,
    tol: float = 1e-8,
) -> NDArray[np.float64]:
    _, d = x.shape
    beta = np.zeros(d, dtype=np.float64)
    penalty = np.full(d, float(l2), dtype=np.float64)
    penalty[0] = 0.0
    for _ in range(max_iter):
        eta = x @ beta
        p = _sigmoid(eta)
        weights = sample_weight * p * (1.0 - p)
        grad = x.T @ (sample_weight * (p - y)) + penalty * beta
        hess = (x * weights[:, None]).T @ x + np.diag(penalty)
        hess.flat[:: d + 1] += 1e-10
        step = np.linalg.solve(hess, grad)
        beta = beta - step
        if float(np.max(np.abs(step))) < tol:
            break
    return beta
