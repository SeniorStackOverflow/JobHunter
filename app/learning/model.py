from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


def _wilson(successes: float, total: float, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 1.0)
    phat = successes / total
    denom = 1.0 + z * z / total
    centre = (phat + z * z / (2.0 * total)) / denom
    margin = z * np.sqrt(phat * (1.0 - phat) / total + z * z / (4.0 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True)
class _Block:
    x_right: float
    mean: float
    n_raw: int
    sum_y: float


@dataclass(frozen=True)
class IsotonicCalibration:
    knots_x: tuple[float, ...]
    knots_p: tuple[float, ...]
    blocks: tuple[_Block, ...]

    def predict(self, raw: float) -> float:
        xs, ps = self.knots_x, self.knots_p
        if raw <= xs[0]:
            return ps[0]
        if raw >= xs[-1]:
            return ps[-1]
        hi = int(np.searchsorted(np.asarray(xs), raw))
        lo = hi - 1
        span = xs[hi] - xs[lo]
        frac = 0.0 if span == 0 else (raw - xs[lo]) / span
        return float(min(1.0, max(0.0, ps[lo] + frac * (ps[hi] - ps[lo]))))

    def interval(self, raw: float) -> tuple[float, float]:
        idx = min(
            range(len(self.blocks)),
            key=lambda i: abs(self.blocks[i].x_right - raw),
        )
        block = self.blocks[idx]
        if block.n_raw < 20:
            return (0.0, 1.0)
        return _wilson(block.sum_y, float(block.n_raw))

    def to_dict(self) -> dict[str, Any]:
        return {
            "knots_x": list(self.knots_x),
            "knots_p": list(self.knots_p),
            "blocks": [
                {"x_right": b.x_right, "mean": b.mean, "n_raw": b.n_raw, "sum_y": b.sum_y}
                for b in self.blocks
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IsotonicCalibration:
        return cls(
            knots_x=tuple(float(v) for v in data["knots_x"]),
            knots_p=tuple(float(v) for v in data["knots_p"]),
            blocks=tuple(
                _Block(float(b["x_right"]), float(b["mean"]), int(b["n_raw"]), float(b["sum_y"]))
                for b in data["blocks"]
            ),
        )


def pava_isotonic(
    raw: NDArray[np.float64],
    y: NDArray[np.float64],
    sample_weight: NDArray[np.float64],
) -> IsotonicCalibration:
    order = np.argsort(raw, kind="stable")
    xs = raw[order].astype(np.float64)
    ys = y[order].astype(np.float64)
    ws = sample_weight[order].astype(np.float64)
    # each block: [x_right, weight, mean, n_raw, sum_y_raw]
    blocks: list[list[float]] = []
    for xi, yi, wi in zip(xs, ys, ws, strict=True):
        blocks.append([float(xi), float(wi), float(yi), 1.0, float(yi)])
        while len(blocks) > 1 and blocks[-2][2] >= blocks[-1][2]:
            x_r2, w2, m2, n2, s2 = blocks.pop()
            _x_r1, w1, m1, n1, s1 = blocks.pop()
            w_new = w1 + w2
            m_new = (w1 * m1 + w2 * m2) / w_new
            blocks.append([x_r2, w_new, m_new, n1 + n2, s1 + s2])
    knots_x = tuple(b[0] for b in blocks)
    knots_p = tuple(min(1.0, max(0.0, b[2])) for b in blocks)
    made = tuple(_Block(b[0], b[2], int(b[3]), b[4]) for b in blocks)
    return IsotonicCalibration(knots_x=knots_x, knots_p=knots_p, blocks=made)


def weighted_auc(y: NDArray[np.float64], p: NDArray[np.float64], w: NDArray[np.float64]) -> float:
    pos = p[y == 1.0]
    pos_w = w[y == 1.0]
    neg = p[y == 0.0]
    neg_w = w[y == 0.0]
    if pos.size == 0 or neg.size == 0:
        return 0.5
    numer = 0.0
    denom = float(pos_w.sum() * neg_w.sum())
    for value, weight in zip(pos, pos_w, strict=True):
        numer += float(weight) * float(neg_w[neg < value].sum() + 0.5 * neg_w[neg == value].sum())
    return numer / denom if denom else 0.5


def weighted_logloss(
    y: NDArray[np.float64], p: NDArray[np.float64], w: NDArray[np.float64]
) -> float:
    clipped = np.clip(p, 1e-6, 1.0 - 1e-6)
    terms = -(y * np.log(clipped) + (1.0 - y) * np.log(1.0 - clipped))
    return float(np.average(terms, weights=w))


def expected_calibration_error(
    y: NDArray[np.float64],
    p: NDArray[np.float64],
    w: NDArray[np.float64],
    *,
    bins: int = 10,
) -> float:
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = float(w.sum())
    if total == 0.0:
        return 0.0
    error = 0.0
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & (p <= hi) if i == bins - 1 else (p >= lo) & (p < hi)
        if not mask.any():
            continue
        bin_w = float(w[mask].sum())
        confidence = float(np.average(p[mask], weights=w[mask]))
        accuracy = float(np.average(y[mask], weights=w[mask]))
        error += (bin_w / total) * abs(confidence - accuracy)
    return error
