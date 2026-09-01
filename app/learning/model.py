from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from app.models.enums import ShadowDecision

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


@dataclass(frozen=True)
class CvResult:
    best_l2: float
    cv_auc: float
    cv_logloss: float
    cv_ece: float
    oof_raw: NDArray[np.float64]
    oof_y: NDArray[np.float64]
    oof_w: NDArray[np.float64]
    cv_ran: bool


def _fold_bounds(n: int, folds: int) -> list[tuple[int, int, int]]:
    chunk = n // (folds + 1)
    bounds = []
    for k in range(1, folds + 1):
        train_end = chunk * k
        test_end = n if k == folds else chunk * (k + 1)
        bounds.append((0, train_end, test_end))
    return bounds


def _oof_for_l2(
    x: NDArray[np.float64],
    y: NDArray[np.float64],
    w: NDArray[np.float64],
    l2: float,
    folds: int,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]] | None:
    raw_parts: list[NDArray[np.float64]] = []
    y_parts: list[NDArray[np.float64]] = []
    w_parts: list[NDArray[np.float64]] = []
    for tr_lo, tr_hi, te_hi in _fold_bounds(len(y), folds):
        y_tr = y[tr_lo:tr_hi]
        if y_tr.size == 0 or y_tr.min() == y_tr.max():
            return None
        beta = fit_l2_logistic(x[tr_lo:tr_hi], y_tr, w[tr_lo:tr_hi], l2)
        raw_parts.append(_sigmoid(x[tr_hi:te_hi] @ beta))
        y_parts.append(y[tr_hi:te_hi])
        w_parts.append(w[tr_hi:te_hi])
    return (
        np.concatenate(raw_parts),
        np.concatenate(y_parts),
        np.concatenate(w_parts),
    )


def time_series_cv(
    x: NDArray[np.float64],
    y: NDArray[np.float64],
    w: NDArray[np.float64],
    *,
    l2_grid: tuple[float, ...] = L2_GRID,
    folds: int = CV_FOLDS,
) -> CvResult:
    neutral = CvResult(
        best_l2=l2_grid[len(l2_grid) // 2],
        cv_auc=0.5,
        cv_logloss=math.log(2.0),
        cv_ece=0.0,
        oof_raw=np.empty(0, dtype=np.float64),
        oof_y=np.empty(0, dtype=np.float64),
        oof_w=np.empty(0, dtype=np.float64),
        cv_ran=False,
    )
    for candidate_folds in (folds, 2):
        if len(y) < candidate_folds + 1:
            continue
        scored: list[tuple[float, float, tuple[NDArray[np.float64], ...]]] = []
        for l2 in l2_grid:
            oof = _oof_for_l2(x, y, w, l2, candidate_folds)
            if oof is None:
                continue
            raw, yy, ww = oof
            scored.append((weighted_logloss(yy, raw, ww), l2, oof))
        if not scored:
            continue
        _, best_l2, (raw, yy, ww) = min(scored, key=lambda item: item[0])
        return CvResult(
            best_l2=best_l2,
            cv_auc=weighted_auc(yy, raw, ww),
            cv_logloss=weighted_logloss(yy, raw, ww),
            cv_ece=expected_calibration_error(yy, raw, ww),
            oof_raw=raw,
            oof_y=yy,
            oof_w=ww,
            cv_ran=True,
        )
    return neutral


@dataclass(frozen=True)
class Contribution:
    label: str
    logit_delta: float


@dataclass(frozen=True)
class Prediction:
    p_approve: float
    ci_low: float
    ci_high: float
    support_ok: bool
    would_decide: ShadowDecision
    top_contributions: tuple[Contribution, ...]


@dataclass(frozen=True)
class TrainedModel:
    feature_spec: dict[str, Any]
    feature_spec_version: str
    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    calibration: IsotonicCalibration
    feature_frequencies: dict[str, int]
    n_labels: int
    n_approved: int
    n_rejected: int
    cv_auc: float
    cv_logloss: float
    cv_ece: float
    cv_ran: bool
    best_l2: float

    def to_json(self) -> dict[str, Any]:
        return {
            "feature_spec": self.feature_spec,
            "feature_spec_version": self.feature_spec_version,
            "feature_names": list(self.feature_names),
            "coefficients": list(self.coefficients),
            "calibration": self.calibration.to_dict(),
            "feature_frequencies": self.feature_frequencies,
            "n_labels": int(self.n_labels),
            "n_approved": int(self.n_approved),
            "n_rejected": int(self.n_rejected),
            "cv_auc": float(self.cv_auc),
            "cv_logloss": float(self.cv_logloss),
            "cv_ece": float(self.cv_ece),
            "cv_ran": bool(self.cv_ran),
            "best_l2": float(self.best_l2),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TrainedModel:
        return cls(
            feature_spec=data["feature_spec"],
            feature_spec_version=data["feature_spec_version"],
            feature_names=tuple(data["feature_names"]),
            coefficients=tuple(float(v) for v in data["coefficients"]),
            calibration=IsotonicCalibration.from_dict(data["calibration"]),
            feature_frequencies={str(k): int(v) for k, v in data["feature_frequencies"].items()},
            n_labels=int(data["n_labels"]),
            n_approved=int(data["n_approved"]),
            n_rejected=int(data["n_rejected"]),
            cv_auc=float(data["cv_auc"]),
            cv_logloss=float(data["cv_logloss"]),
            cv_ece=float(data["cv_ece"]),
            cv_ran=bool(data.get("cv_ran", True)),
            best_l2=float(data["best_l2"]),
        )


def build_trained_model(
    *,
    feature_spec: dict[str, Any],
    feature_spec_version: str,
    feature_names: Sequence[str],
    x: NDArray[np.float64],
    y: NDArray[np.float64],
    w: NDArray[np.float64],
    feature_frequencies: dict[str, int],
) -> TrainedModel:
    cv = time_series_cv(x, y, w)
    beta = fit_l2_logistic(x, y, w, cv.best_l2)
    if cv.oof_raw.size > 0:
        calibration = pava_isotonic(cv.oof_raw, cv.oof_y, cv.oof_w)
    else:
        calibration = pava_isotonic(_sigmoid(x @ beta), y, w)
    return TrainedModel(
        feature_spec=feature_spec,
        feature_spec_version=feature_spec_version,
        feature_names=tuple(feature_names),
        coefficients=tuple(float(v) for v in beta),
        calibration=calibration,
        feature_frequencies=dict(feature_frequencies),
        n_labels=len(y),
        n_approved=int(y.sum()),
        n_rejected=int((1.0 - y).sum()),
        cv_auc=cv.cv_auc,
        cv_logloss=cv.cv_logloss,
        cv_ece=cv.cv_ece,
        cv_ran=cv.cv_ran,
        best_l2=cv.best_l2,
    )


def predict(
    model: TrainedModel,
    *,
    row: NDArray[np.float64],
    present_values: Sequence[str],
    contribution_labels: Mapping[str, str],
) -> Prediction:
    beta = np.asarray(model.coefficients, dtype=np.float64)
    full = np.concatenate([np.array([1.0]), row])
    raw = float(_sigmoid(np.array([full @ beta]))[0])
    p_approve = model.calibration.predict(raw)
    ci_low, ci_high = model.calibration.interval(raw)
    support_ok = all(
        model.feature_frequencies.get(value, 0) >= MIN_FEATURE_SUPPORT for value in present_values
    )
    narrow = (ci_high - ci_low) <= CI_MAX_WIDTH
    if not model.cv_ran:
        # CV could not run (single-class training prefix): the point estimate is
        # calibrated only in-sample, so it must never drive a shadow decision.
        decision = ShadowDecision.ABSTAIN
    elif support_ok and narrow and p_approve >= SHADOW_APPROVE_P:
        decision = ShadowDecision.APPROVE
    elif support_ok and narrow and p_approve <= SHADOW_REJECT_P:
        decision = ShadowDecision.REJECT
    else:
        decision = ShadowDecision.ABSTAIN
    contributions = sorted(
        (
            Contribution(
                label=contribution_labels.get(name, name),
                logit_delta=float(beta[i + 1] * row[i]),
            )
            for i, name in enumerate(model.feature_names[1:])
            if row[i] != 0.0
        ),
        key=lambda c: abs(c.logit_delta),
        reverse=True,
    )
    return Prediction(
        p_approve=p_approve,
        ci_low=ci_low,
        ci_high=ci_high,
        support_ok=support_ok,
        would_decide=decision,
        top_contributions=tuple(contributions[:3]),
    )
