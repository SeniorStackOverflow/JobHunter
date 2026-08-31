# Learning Autonomy — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the calibrated interpretable review-learning model and a shadow-mode
evaluation loop, so the model's would-be decisions are measured against the operator's
real decisions — with zero change to any current behaviour.

**Architecture:** New self-contained modules under `app/learning/` (`model.py` numpy
estimators, `features.py` feature extraction, `training.py` orchestration,
`shadow.py` shadow-outcome recording + scorecard). Two new append-only tables
(`LearningModelVersion`, `LearningShadowOutcome`). Two new nightly/periodic Celery
tasks. One new read-only MCP tool and one daily-report block. The existing
count/proposal/ordering code (`_summarize_events`, `_score`,
`ReviewLearningService.score`) is **not touched**.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async, Alembic, Celery, FastMCP,
`numpy` (new core dependency), pytest / pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-31-learning-autonomy-design.md` (this plan
implements **Phase 1** from §13 only).

## Global Constraints

- Python 3.12 semantics. Before handoff: `uv run ruff format --check .`,
  `uv run ruff check .`, `uv run mypy app fixture_site`, `uv run pytest`,
  `uv run alembic check`. Never enable real email delivery or live crawling in tests.
- **New dependency:** `numpy>=2,<3` in `[project].dependencies`. **No** `scikit-learn` /
  `scipy` — estimators are hand-written on numpy and frozen against fixtures.
- **Label schema version is unchanged** (`FEATURE_SCHEMA_VERSION = "review-v2"`).
  `record_decision` only *enriches* `feature_snapshot` with additive `numeric` /
  `context` keys. The model's own extraction version is a **separate** constant
  `FEATURE_SPEC_VERSION = "features-v3"` in `app/learning/features.py`.
- **Phase 1 is purely additive.** No edits to `app/policies/engine.py`,
  `app/email/service.py`, `app/applications/service.py` behaviour, or to
  `_summarize_events` / `_score` / `ReviewLearningService.score` /
  `ReviewLearningService.summary`. `tests/unit/test_review_learning.py` must stay green
  and unmodified.
- **Segment:** Phase 1 trains exactly one model per profile with
  `segment_key = "global"`.
- **All tunables are module constants in Phase 1** (Phase 2 promotes them to
  `Settings`). In `app/learning/features.py`: `FEATURE_SPEC_VERSION = "features-v3"`,
  `HALF_LIFE_DAYS = 120`, `MIN_VOCAB_SUPPORT = 3`, `TITLE_VOCAB_CAP = 40`,
  `OTHER_VOCAB_CAP = 24`, `MODEL_MIN_LABELS = 40`, `MODEL_MIN_PER_OUTCOME = 8`,
  `MODEL_ELIGIBLE_SCHEMAS = frozenset({"review-v2"})`. In `app/learning/model.py`:
  `SHADOW_APPROVE_P = 0.90`, `SHADOW_REJECT_P = 0.12`, `CI_MAX_WIDTH = 0.15`,
  `MIN_FEATURE_SUPPORT = 5`, `L2_GRID = (0.03, 0.1, 0.3, 1.0, 3.0)`, `CV_FOLDS = 4`.
  (`MIN_FEATURE_SUPPORT` lives in `model.py` because `predict` is its only consumer
  and `model.py` must not import `features.py`.)
- Migrations are **hand-written** to match the models exactly; `alembic check` must
  report no diff. Enum columns use `native_enum=False` (follow existing entities).
- Commit after every task with a `feat:` / `test:` / `chore:` prefixed message.
- Work happens on branch `feature/learning-autonomy` (already checked out).

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `app/learning/model.py` | Pure-numpy estimators: `fit_l2_logistic` (IRLS), `pava_isotonic` + `IsotonicCalibration`, metrics (`weighted_auc`, `weighted_logloss`, `expected_calibration_error`), `time_series_cv`, `TrainedModel`, `Prediction`, `predict`, JSON (de)serialisation. No DB imports. |
| `app/learning/features.py` | Tunable constants; `FeatureSpec`, `ExtractedFeatures`; `build_feature_spec`, `extract_from_event`, `extract_live`, `build_snapshot_extras`, `vectorize`, `build_matrix`. Imports models but not sessions. |
| `app/learning/training.py` | `train_profile(session, profile_id)` — load `review-v2` labels, build spec + matrix, cross-validate, fit, persist a `LearningModelVersion`. `train_all_profiles()` module entrypoint for the Celery task. |
| `app/learning/shadow.py` | `record_shadow_outcomes(session)` (predict on `PENDING_REVIEW` apps, upsert `LearningShadowOutcome`), `attach_human_decision(session, application, outcome, reason)` (fill `human_decision`/`agreed`), `shadow_scorecard(session, profile_id, window_days=90)`. `record_learning_shadow()` module entrypoint. |
| `tests/unit/test_learning_model.py` | Estimators + `predict` + JSON round-trip. |
| `tests/unit/test_learning_features.py` | Spec vocab, event/live extraction, causal masking, time-decay, vectorisation. |
| `tests/unit/test_learning_shadow.py` | `attach_human_decision` agreement logic, scorecard aggregation (in-memory rows). |
| `tests/integration/test_learning_training_and_shadow.py` | End-to-end on `sqlite_session_factory`: seed labels → train → predict on a pending application → record shadow → resolve human decision → scorecard. |
| `migrations/versions/<rev>_learning_model_and_shadow.py` | `learning_model_versions` + `learning_shadow_outcomes` tables. |

**Modified:**

| File | Change |
|---|---|
| `pyproject.toml` | Add `numpy>=2,<3`; add mypy override for the two numeric modules. |
| `app/models/enums.py` | Add `ShadowDecision` (`approve` / `reject` / `abstain`). |
| `app/models/entities.py` | Add `LearningModelVersion`, `LearningShadowOutcome`. |
| `app/models/__init__.py` | Export the two new entities. |
| `app/learning/service.py` | `record_decision`: load `JobPreference`, merge `build_snapshot_extras(...)` into `feature_snapshot`, then call `attach_human_decision(...)`. Nothing else. |
| `app/learning/__init__.py` | Export new public names. |
| `app/scheduler/tasks.py` | `train_learning_models_task`, `record_learning_shadow_task`. |
| `app/scheduler/celery_app.py` | `beat_schedule` + `task_routes` entries for both. |
| `app/reports/service.py` | Add `learning_shadow` list to the daily summary. |
| `app/mcp/server.py` | Add read-only `get_learning_model_status` tool. |
| `docs/review-learning.md` | Short "Модель v3 и shadow-режим" section. |

---

## Task 1: Add numpy dependency and mypy config

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `numpy` importable in `app.learning.*`; relaxed `warn_return_any` for the
  two numeric modules.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, in `[project].dependencies`, insert alphabetically (after
`mcp>=1.13,<2`):

```toml
  "numpy>=2,<3",
```

- [ ] **Step 2: Add the mypy override**

Append to `pyproject.toml` after the existing `[[tool.mypy.overrides]]` block:

```toml
[[tool.mypy.overrides]]
module = ["app.learning.model", "app.learning.features", "app.learning.training"]
warn_return_any = false
```

- [ ] **Step 3: Sync and verify import**

Run: `uv sync --extra dev && uv run python -c "import numpy; print(numpy.__version__)"`
Expected: prints a `2.x` version, no error.

- [ ] **Step 4: Verify nothing else broke**

Run: `uv run ruff check . && uv run mypy app`
Expected: PASS (no new errors).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add numpy dependency for learning model"
```

---

## Task 2: IRLS L2-logistic regression

**Files:**
- Create: `app/learning/model.py`
- Test: `tests/unit/test_learning_model.py`

**Interfaces:**
- Produces:
  `fit_l2_logistic(X: NDArray[np.float64], y: NDArray[np.float64], sample_weight: NDArray[np.float64], l2: float, *, max_iter: int = 100, tol: float = 1e-8) -> NDArray[np.float64]`
  — `X` includes an intercept column at index 0 (caller's responsibility); the L2
  penalty is applied to every coefficient **except** index 0; returns the coefficient
  vector of length `X.shape[1]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_learning_model.py
import numpy as np

from app.learning.model import fit_l2_logistic


def _design(rows: list[tuple[float, float]]) -> np.ndarray:
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
    assert abs(strong[1]) < 0.1  # ridge-shrunk value for l2=1000 on this data is ~0.057


def test_sample_weight_moves_the_intercept() -> None:
    x = _design([(0.0,)] * 10)
    y = np.array([1.0] * 7 + [0.0] * 3)
    heavy_positive = np.array([5.0] * 7 + [1.0] * 3)

    beta = fit_l2_logistic(x, y, heavy_positive, l2=0.0)

    # weighted positive rate 35/38 -> logit ~ 2.45
    assert 2.0 < beta[0] < 3.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_learning_model.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.learning.model'`.

- [ ] **Step 3: Implement**

```python
# app/learning/model.py
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_learning_model.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/learning/model.py tests/unit/test_learning_model.py
git commit -m "feat: add IRLS L2-logistic estimator for review learning"
```

---

## Task 2b: `app/learning/model.py` — package `__init__` guard

(Folded into Task 2 if `app/learning/__init__.py` already imports cleanly — it does.
No separate step; `model.py` is imported directly by tests.)

---

## Task 3: PAVA isotonic calibration with Wilson intervals

**Files:**
- Modify: `app/learning/model.py`
- Test: `tests/unit/test_learning_model.py`

**Interfaces:**
- Produces:
  - `pava_isotonic(raw: NDArray[np.float64], y: NDArray[np.float64], sample_weight: NDArray[np.float64]) -> IsotonicCalibration`
  - `IsotonicCalibration.predict(raw: float) -> float` — monotone non-decreasing,
    linear interpolation between knots, clamped to `[0, 1]`.
  - `IsotonicCalibration.interval(raw: float) -> tuple[float, float]` — 95% Wilson
    interval of the nearest calibration block; widened to `(0.0, 1.0)` when that block
    has `< 20` raw points.
  - `IsotonicCalibration.to_dict()` / `IsotonicCalibration.from_dict(data)`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_learning_model.py
from app.learning.model import IsotonicCalibration, pava_isotonic


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_learning_model.py -k isotonic -v`
Expected: FAIL — `ImportError: cannot import name 'IsotonicCalibration'`.

- [ ] **Step 3: Implement**

```python
# app/learning/model.py — append
from dataclasses import dataclass
from typing import Any


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
            x_r1, w1, m1, n1, s1 = blocks.pop()
            w_new = w1 + w2
            m_new = (w1 * m1 + w2 * m2) / w_new
            blocks.append([x_r2, w_new, m_new, n1 + n2, s1 + s2])
    knots_x = tuple(b[0] for b in blocks)
    knots_p = tuple(min(1.0, max(0.0, b[2])) for b in blocks)
    made = tuple(_Block(b[0], b[2], int(b[3]), b[4]) for b in blocks)
    return IsotonicCalibration(knots_x=knots_x, knots_p=knots_p, blocks=made)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_learning_model.py -v`
Expected: PASS (all model tests so far).

- [ ] **Step 5: Commit**

```bash
git add app/learning/model.py tests/unit/test_learning_model.py
git commit -m "feat: add PAVA isotonic calibration with Wilson intervals"
```

---

## Task 4: Calibration and ranking metrics

**Files:**
- Modify: `app/learning/model.py`
- Test: `tests/unit/test_learning_model.py`

**Interfaces:**
- Produces:
  - `weighted_auc(y: NDArray[np.float64], p: NDArray[np.float64], w: NDArray[np.float64]) -> float`
  - `weighted_logloss(y: NDArray[np.float64], p: NDArray[np.float64], w: NDArray[np.float64]) -> float`
  - `expected_calibration_error(y: NDArray[np.float64], p: NDArray[np.float64], w: NDArray[np.float64], *, bins: int = 10) -> float`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_learning_model.py
from app.learning.model import (
    expected_calibration_error,
    weighted_auc,
    weighted_logloss,
)


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_learning_model.py -k "auc or logloss or ece" -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

```python
# app/learning/model.py — append
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_learning_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/learning/model.py tests/unit/test_learning_model.py
git commit -m "feat: add AUC, log-loss and ECE metrics for review learning"
```

---

## Task 5: Time-series cross-validation and L2 selection

**Files:**
- Modify: `app/learning/model.py`
- Test: `tests/unit/test_learning_model.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class CvResult: best_l2: float; cv_auc: float; cv_logloss: float; cv_ece: float; oof_raw: NDArray[np.float64]; oof_y: NDArray[np.float64]; oof_w: NDArray[np.float64]`
  - `time_series_cv(x: NDArray[np.float64], y: NDArray[np.float64], w: NDArray[np.float64], *, l2_grid: tuple[float, ...] = L2_GRID, folds: int = CV_FOLDS) -> CvResult`
    — rows are assumed pre-sorted oldest→newest. Expanding-window folds over the last
    `folds` equal chunks; picks the `l2` with the lowest mean out-of-fold weighted
    log-loss; returns pooled OOF predictions (pre-calibration `raw = sigmoid(eta)`) and
    the OOF metrics at that `l2`. With fewer than `folds + 1` rows or a degenerate
    single-class training prefix, falls back to `folds = 2`; if still impossible,
    returns `best_l2 = l2_grid[len(l2_grid)//2]` and `cv_*` = neutral
    (`0.5, log(2), 0.0`) with empty OOF arrays.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_learning_model.py
from app.learning.model import CvResult, time_series_cv


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_learning_model.py -k cv -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

```python
# app/learning/model.py — append
import math


@dataclass(frozen=True)
class CvResult:
    best_l2: float
    cv_auc: float
    cv_logloss: float
    cv_ece: float
    oof_raw: NDArray[np.float64]
    oof_y: NDArray[np.float64]
    oof_w: NDArray[np.float64]


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
        oof_raw=np.empty(0),
        oof_y=np.empty(0),
        oof_w=np.empty(0),
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
        )
    return neutral
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_learning_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/learning/model.py tests/unit/test_learning_model.py
git commit -m "feat: add time-series CV and L2 selection for review learning"
```

---

## Task 6: `TrainedModel`, `Prediction`, `predict`, JSON round-trip

**Files:**
- Modify: `app/learning/model.py`
- Test: `tests/unit/test_learning_model.py`

**Interfaces:**
- Consumes: `fit_l2_logistic`, `pava_isotonic`, `time_series_cv`, metrics (this module);
  `FeatureSpec` will be defined in Task 7 — **`model.py` must not import `features.py`**.
  `TrainedModel` therefore stores the spec as an opaque `dict[str, Any]` under
  `feature_spec` and never interprets it.
- Produces:
  - `@dataclass(frozen=True) class Contribution: label: str; logit_delta: float`
  - `@dataclass(frozen=True) class Prediction: p_approve: float; ci_low: float; ci_high: float; support_ok: bool; would_decide: ShadowDecision; top_contributions: tuple[Contribution, ...]`
  - `@dataclass(frozen=True) class TrainedModel` with fields: `feature_spec: dict[str, Any]`,
    `feature_names: tuple[str, ...]` (length = coeff length, index 0 == `"__intercept__"`),
    `coefficients: tuple[float, ...]`, `calibration: IsotonicCalibration`,
    `feature_frequencies: dict[str, int]`, `n_labels: int`, `n_approved: int`,
    `n_rejected: int`, `cv_auc: float`, `cv_logloss: float`, `cv_ece: float`,
    `best_l2: float`, `feature_spec_version: str`.
  - `TrainedModel.to_json() -> dict[str, Any]` / `TrainedModel.from_json(data) -> TrainedModel`
  - `build_trained_model(*, feature_spec: dict[str, Any], feature_spec_version: str, feature_names: Sequence[str], x: NDArray[np.float64], y: NDArray[np.float64], w: NDArray[np.float64], feature_frequencies: dict[str, int]) -> TrainedModel`
    — runs CV, fits final `fit_l2_logistic` on **all** rows at `best_l2`, calibrates on
    the pooled OOF predictions (falls back to calibrating on in-sample predictions when
    OOF is empty), fills the metrics.
  - `predict(model: TrainedModel, *, row: NDArray[np.float64], present_values: Sequence[str], contribution_labels: Mapping[str, str]) -> Prediction`
    — `row` is the already-vectorised feature row **without** the intercept slot;
    `present_values` are the categorical feature values on this job (for the support
    check); `contribution_labels` maps `feature_name -> human label`. `support_ok` is
    `all(model.feature_frequencies.get(v, 0) >= MIN_FEATURE_SUPPORT for v in present_values)`
    (`MIN_FEATURE_SUPPORT` is a module constant of `model.py`, defined in Task 2).
    `would_decide`: `APPROVE` if
    `p_approve >= SHADOW_APPROVE_P and (ci_high - ci_low) <= CI_MAX_WIDTH and support_ok`;
    `REJECT` if `p_approve <= SHADOW_REJECT_P and (ci_high - ci_low) <= CI_MAX_WIDTH and support_ok`;
    else `ABSTAIN`. `top_contributions`: the 3 features with the largest `abs(coef * value)`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_learning_model.py
from app.learning.model import Prediction, TrainedModel, build_trained_model, predict
from app.models.enums import ShadowDecision


def _fitted() -> TrainedModel:
    rng = np.random.default_rng(1)
    feature = rng.normal(size=300)
    logit = 2.0 * feature
    y = (rng.uniform(size=300) < 1.0 / (1.0 + np.exp(-logit))).astype(np.float64)
    x = np.column_stack([np.ones(300), feature])
    return build_trained_model(
        feature_spec={"version": "features-v3"},
        feature_spec_version="features-v3",
        feature_names=("__intercept__", "signal"),
        x=x,
        y=y,
        w=np.ones(300),
        feature_frequencies={"cat:warehouses": 40},
    )


def test_trained_model_predicts_high_probability_for_a_strong_positive_row() -> None:
    model = _fitted()

    result = predict(
        model,
        row=np.array([3.0]),
        present_values=["cat:warehouses"],
        contribution_labels={"signal": "категория: склад"},
    )

    assert isinstance(result, Prediction)
    assert result.p_approve > 0.8
    assert result.support_ok is True
    assert result.top_contributions[0].label == "категория: склад"


def test_novel_feature_value_forces_abstain() -> None:
    model = _fitted()

    result = predict(
        model,
        row=np.array([3.0]),
        present_values=["cat:brand-new"],
        contribution_labels={},
    )

    assert result.support_ok is False
    assert result.would_decide is ShadowDecision.ABSTAIN


def test_trained_model_round_trips_through_json() -> None:
    model = _fitted()

    restored = TrainedModel.from_json(model.to_json())

    assert restored.coefficients == model.coefficients
    assert restored.cv_auc == model.cv_auc
    same = predict(
        restored,
        row=np.array([1.0]),
        present_values=["cat:warehouses"],
        contribution_labels={},
    )
    assert (
        same.p_approve
        == predict(
            model, row=np.array([1.0]), present_values=["cat:warehouses"], contribution_labels={}
        ).p_approve
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_learning_model.py -k "trained or novel" -v`
Expected: FAIL — `ImportError` for `TrainedModel` / `ShadowDecision`.

- [ ] **Step 3a: Add the `ShadowDecision` enum**

```python
# app/models/enums.py — append
class ShadowDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"
```

- [ ] **Step 3b: Implement the model API**

```python
# app/learning/model.py — append
from collections.abc import Mapping, Sequence

from app.models.enums import ShadowDecision


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
    best_l2: float

    def to_json(self) -> dict[str, Any]:
        return {
            "feature_spec": self.feature_spec,
            "feature_spec_version": self.feature_spec_version,
            "feature_names": list(self.feature_names),
            "coefficients": list(self.coefficients),
            "calibration": self.calibration.to_dict(),
            "feature_frequencies": self.feature_frequencies,
            "n_labels": self.n_labels,
            "n_approved": self.n_approved,
            "n_rejected": self.n_rejected,
            "cv_auc": self.cv_auc,
            "cv_logloss": self.cv_logloss,
            "cv_ece": self.cv_ece,
            "best_l2": self.best_l2,
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
        n_labels=int(len(y)),
        n_approved=int(y.sum()),
        n_rejected=int((1.0 - y).sum()),
        cv_auc=cv.cv_auc,
        cv_logloss=cv.cv_logloss,
        cv_ece=cv.cv_ece,
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
    full = np.concatenate([[1.0], row])
    raw = float(_sigmoid(np.array([full @ beta]))[0])
    p_approve = model.calibration.predict(raw)
    ci_low, ci_high = model.calibration.interval(raw)
    support_ok = all(
        model.feature_frequencies.get(value, 0) >= MIN_FEATURE_SUPPORT for value in present_values
    )
    narrow = (ci_high - ci_low) <= CI_MAX_WIDTH
    if support_ok and narrow and p_approve >= SHADOW_APPROVE_P:
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_learning_model.py -v && uv run mypy app/learning/model.py app/models/enums.py`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/learning/model.py app/models/enums.py tests/unit/test_learning_model.py
git commit -m "feat: add TrainedModel, prediction and JSON persistence for review learning"
```

---

## Task 7: Feature spec and vocabulary

**Files:**
- Create: `app/learning/features.py`
- Test: `tests/unit/test_learning_features.py`

**Interfaces:**
- Consumes: `app.learning.service._feature_snapshot`, `review_job_input`,
  `_REJECTION_DIMENSIONS`, `_ALL_DIMENSIONS`, `_DIMENSION_LABELS`, `_feature_label`
  (existing, importable). `ReviewFeedbackEvent` model.
- Produces (constants): `FEATURE_SPEC_VERSION = "features-v3"`, `HALF_LIFE_DAYS = 120`,
  `MIN_VOCAB_SUPPORT = 3`, `TITLE_VOCAB_CAP = 40`,
  `OTHER_VOCAB_CAP = 24`, `MODEL_MIN_LABELS = 40`, `MODEL_MIN_PER_OUTCOME = 8`,
  `MODEL_ELIGIBLE_SCHEMAS = frozenset({"review-v2"})`,
  `NUMERIC_NAMES: tuple[str, ...]`, `AGE_BUCKETS: tuple[str, ...]`.
  (`MIN_FEATURE_SUPPORT` is **not** here — it lives in `model.py`, Task 2.)
- Produces (types):
  - `@dataclass(frozen=True) class FeatureSpec: version: str; categorical: dict[str, tuple[str, ...]]; source_keys: tuple[str, ...]`
    with `.feature_names() -> tuple[str, ...]` (ordered, index 0 == `"__intercept__"`)
    and `.to_dict()` / `.from_dict(data)`.
  - `@dataclass(frozen=True) class ExtractedFeatures: categorical: dict[str, list[str]]; numeric: dict[str, float]; source_key: str; age_bucket: str; active_dimensions: frozenset[str]`
- Produces (functions): `build_feature_spec(events: Sequence[ReviewFeedbackEvent]) -> FeatureSpec`.

Feature-name scheme (stable strings, also the `feature_frequencies` / `present_values`
keys and the causal-mask unit):
- categorical: `f"{dimension}:{value}"` e.g. `"category:warehouses"`, `"title:picker"`.
- dimension-observed: `f"obs:{dimension}"` (one per dimension in `_ALL_DIMENSIONS`).
- numeric: the entries of `NUMERIC_NAMES` verbatim.
- context: `f"source:{key}"` per learned source key + `"source:__other__"`;
  `f"age:{bucket}"` per bucket in `AGE_BUCKETS`.

`NUMERIC_NAMES = ("overall_fit", "resume_fit", "preference_fit", "llm_scores_missing",
"n_missing_requirements", "n_risks", "salary_gap", "salary_missing", "llm_auto_apply",
"llm_prepare", "llm_skip", "llm_block", "llm_decision_missing")`

`AGE_BUCKETS = ("0-3", "4-7", "8-30", "31+", "unknown")`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_learning_features.py
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_learning_features.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# app/learning/features.py
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_learning_features.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/learning/features.py tests/unit/test_learning_features.py
git commit -m "feat: add learning feature spec and vocabulary builder"
```

---

## Task 8: Feature extraction from events and live jobs

**Files:**
- Modify: `app/learning/features.py`
- Test: `tests/unit/test_learning_features.py`

**Interfaces:**
- Consumes: `FeatureSpec`, `ExtractedFeatures` (Task 7);
  `app.learning.service._feature_snapshot`, `review_job_input`, `_REJECTION_DIMENSIONS`;
  `SourceJob`, `MatchEvaluation`, `JobPreference` models;
  `app.models.enums.MatchDecision`.
- Produces:
  - `extract_from_event(event: ReviewFeedbackEvent) -> tuple[ExtractedFeatures, float]`
    — second element is the outcome label (`1.0` approved / `0.0` rejected). Reads
    `feature_snapshot["features"]` for categorical, `feature_snapshot.get("numeric", {})`
    for numeric (missing → neutral fill + `*_missing` = 1.0),
    `feature_snapshot.get("context", {})` for `source_key` / `age_bucket`
    (missing → `"__other__"` / `"unknown"`), `feature_snapshot["learning_dimensions"]`
    for `active_dimensions` (∩ `_ALL_DIMENSIONS`).
  - `build_snapshot_extras(job: SourceJob, evaluation: MatchEvaluation | None, preference: JobPreference | None) -> dict[str, Any]`
    — returns `{"numeric": {...}, "context": {"source_key": ..., "age_bucket": ...}}`
    for merging into `feature_snapshot` at label-write time.
  - `extract_live(job: SourceJob, evaluation: MatchEvaluation, preference: JobPreference, *, source_key: str) -> ExtractedFeatures`
    — categorical via `_feature_snapshot(review_job_input(job))`; numeric via the same
    logic as `build_snapshot_extras`; `active_dimensions = frozenset(_ALL_DIMENSIONS)`.
  - helper `numeric_from_evaluation(evaluation: MatchEvaluation | None, job: SourceJob, preference: JobPreference | None) -> dict[str, float]` (shared by the two above).
  - helper `age_bucket_for(job: SourceJob) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_learning_features.py
from decimal import Decimal

from app.learning.features import build_snapshot_extras, extract_from_event, numeric_from_evaluation
from app.models.entities import JobPreference, MatchEvaluation, SourceJob
from app.models.enums import MatchDecision, ReviewOutcome, ReviewReason


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
    assert features.numeric["salary_missing"] == 0.0  # numeric present -> not missing


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_learning_features.py -k "extraction or numeric or extras or missing_numeric" -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

```python
# app/learning/features.py — append
from datetime import UTC, datetime
from decimal import Decimal

from app.learning.service import _feature_snapshot, review_job_input
from app.models.entities import JobPreference, MatchEvaluation, SourceJob
from app.models.enums import MatchDecision

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


def age_bucket_for(job: SourceJob) -> str:
    published = job.published_at
    if published is None:
        return "unknown"
    days = (datetime.now(UTC) - published).days
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
        for name in NUMERIC_NAMES:
            if name in stored_numeric:
                numeric[name] = float(stored_numeric[name])
        if "overall_fit" in stored_numeric and stored_numeric["overall_fit"] is not None:
            # a normalised value (<=1) is already stored; a raw 0..100 is legacy.
            if numeric["overall_fit"] > 1.0:
                for key in ("overall_fit", "resume_fit", "preference_fit"):
                    numeric[key] = _clip(numeric[key] / 100.0, 0.0, 1.0)
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
```

Note: `_feature_snapshot` returns keys `category, title, city, area, schedule, workplace,
experience, company, salary` — matching `_ALL_DIMENSIONS`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_learning_features.py -v && uv run mypy app/learning/features.py`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/learning/features.py tests/unit/test_learning_features.py
git commit -m "feat: extract learning features from events and live jobs"
```

---

## Task 9: Vectorisation and training-matrix assembly

**Files:**
- Modify: `app/learning/features.py`
- Test: `tests/unit/test_learning_features.py`

**Interfaces:**
- Consumes: `FeatureSpec`, `ExtractedFeatures`, `extract_from_event` (Tasks 7–8); numpy.
- Produces:
  - `vectorize(spec: FeatureSpec, features: ExtractedFeatures) -> NDArray[np.float64]`
    — length `len(spec.feature_names()) - 1` (no intercept slot). Categorical one-hots
    are set to `1.0` only for values in the spec vocab **and** whose dimension is in
    `features.active_dimensions` (causal mask). `obs:{dimension}` = `1.0` iff the
    dimension is active. Numeric entries copied in `NUMERIC_NAMES` order. `source:{key}`
    one-hot (`source:__other__` when the key is not in the spec). `age:{bucket}` one-hot.
  - `present_values(spec: FeatureSpec, features: ExtractedFeatures) -> list[str]`
    — the `f"{dimension}:{value}"` keys actually present on this job **and** in the
    spec vocab (used for the support check; unaffected by the causal mask).
  - `build_matrix(events: Sequence[ReviewFeedbackEvent], spec: FeatureSpec, *, now: datetime | None = None) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64], dict[str, int]]`
    — returns `(X, y, w, feature_frequencies)`. `X` includes the intercept column at 0.
    Rows come only from events with `feature_schema_version in MODEL_ELIGIBLE_SCHEMAS`
    and `learning_eligible is True`, oldest→newest. `w = 0.5 ** (age_days / HALF_LIFE_DAYS)`
    with `age_days = max(0, (now - event.created_at).days)`, `now` defaults to
    `datetime.now(UTC)`. `feature_frequencies` counts, over the same rows,
    `f"{dimension}:{value}"` occurrences (mask-independent).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_learning_features.py
import numpy as np

from app.learning.features import build_matrix, present_values, vectorize


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
    )
    features, _ = extract_from_event(_rejected_event(["salary"], ReviewReason.SALARY))
    # rejected-for-salary event: category present in snapshot but not active
    names = spec.feature_names()[1:]
    row = vectorize(spec, features)

    idx_obs_category = names.index("obs:category")
    assert row[idx_obs_category] == 0.0  # category dimension masked out


def test_matrix_weights_decay_with_age() -> None:
    old = _approved(["category"], category=["warehouses"])
    old.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    recent = _approved(["category"], category=["warehouses"])
    recent.created_at = datetime(2026, 8, 20, tzinfo=UTC)
    spec = build_feature_spec(
        [old, recent]
        + [_approved(["category"], category=["warehouses"]) for _ in range(MIN_VOCAB_SUPPORT)]
    )

    x, y, w, freq = build_matrix([old, recent], spec, now=datetime(2026, 8, 21, tzinfo=UTC))

    assert x.shape[0] == 2
    assert x[:, 0].tolist() == [1.0, 1.0]  # intercept column
    assert w[0] < w[1]
    assert freq["category:warehouses"] == 2


def test_present_values_ignores_out_of_vocab() -> None:
    spec = build_feature_spec(
        [_approved(["category"], category=["warehouses"]) for _ in range(MIN_VOCAB_SUPPORT)]
    )
    features, _ = extract_from_event(_approved(["category"], category=["warehouses", "unlisted"]))

    assert present_values(spec, features) == ["category:warehouses"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_learning_features.py -k "vectorize or matrix or present" -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

```python
# app/learning/features.py — append
import numpy as np
from numpy.typing import NDArray


def present_values(spec: FeatureSpec, features: ExtractedFeatures) -> list[str]:
    found: list[str] = []
    for dimension in _ALL_DIMENSIONS:
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
    moment = now or datetime.now(UTC)
    usable = sorted(
        (
            event
            for event in events
            if event.learning_eligible and event.feature_schema_version in MODEL_ELIGIBLE_SCHEMAS
        ),
        key=lambda e: e.created_at,
    )
    rows: list[NDArray[np.float64]] = []
    labels: list[float] = []
    weights: list[float] = []
    frequencies: Counter[str] = Counter()
    for event in usable:
        features, label = extract_from_event(event)
        rows.append(vectorize(spec, features))
        labels.append(label)
        age_days = max(0, (moment - event.created_at).days)
        weights.append(0.5 ** (age_days / HALF_LIFE_DAYS))
        for key in present_values(spec, features):
            frequencies[key] += 1
    if not rows:
        width = len(spec.feature_names())
        empty = np.empty((0, width), dtype=np.float64)
        return empty, np.empty(0), np.empty(0), {}
    body = np.vstack(rows)
    x = np.column_stack([np.ones(len(rows)), body])
    return x, np.array(labels), np.array(weights), dict(frequencies)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_learning_features.py -v && uv run ruff check app/learning && uv run mypy app/learning/features.py`
Expected: PASS; clean.

- [ ] **Step 5: Commit**

```bash
git add app/learning/features.py tests/unit/test_learning_features.py
git commit -m "feat: vectorise learning features into a weighted training matrix"
```

---

## Task 10: New entities, enum export, migration

**Files:**
- Modify: `app/models/entities.py`, `app/models/__init__.py`
- Create: `migrations/versions/<rev>_learning_model_and_shadow.py`
- Test: `tests/integration/test_learning_training_and_shadow.py` (schema-creation smoke only in this task)

**Interfaces:**
- Produces SQLAlchemy models:
  - `LearningModelVersion(UUIDPrimaryKeyMixin, Base)` — table `learning_model_versions`:
    `profile_id` (FK `user_profiles.id` CASCADE, index), `segment_key: str(64)`,
    `feature_spec_version: str(32)`, `algorithm: str(32)`,
    `payload: JSON` (the `TrainedModel.to_json()` dict), `n_labels: int`,
    `n_approved: int`, `n_rejected: int`, `cv_auc: float`, `cv_logloss: float`,
    `cv_ece: float`, `trained_at: datetime tz default utcnow`.
    `UniqueConstraint("profile_id", "segment_key", "trained_at", name="uq_learning_model_versions_identity")`.
  - `LearningShadowOutcome(UUIDPrimaryKeyMixin, Base)` — table `learning_shadow_outcomes`:
    `profile_id` (FK CASCADE, index), `application_id` (FK `applications.id` CASCADE, index),
    `model_version_id` (FK `learning_model_versions.id` SET NULL, nullable),
    `segment_key: str(64)`, `p_approve: float`, `ci_low: float`, `ci_high: float`,
    `support_ok: bool`, `would_decide` (`enum_column(ShadowDecision)`),
    `human_decision` (`enum_column(ReviewOutcome)`, nullable),
    `human_reason` (`enum_column(ReviewReason)`, nullable), `agreed: bool | None`,
    `sampled: bool default False`, `created_at: datetime tz default utcnow index`.
    `UniqueConstraint("application_id", "model_version_id", name="uq_learning_shadow_outcomes_identity")`.
- Produces: both names exported from `app.models` and `app.models.__init__.__all__`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_learning_training_and_shadow.py
import pytest
from sqlalchemy import select

from app.models.entities import LearningModelVersion, LearningShadowOutcome

pytestmark = pytest.mark.asyncio


async def test_new_learning_tables_are_created(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        assert (await session.scalars(select(LearningModelVersion))).all() == []
        assert (await session.scalars(select(LearningShadowOutcome))).all() == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_learning_training_and_shadow.py -v`
Expected: FAIL — `ImportError: cannot import name 'LearningModelVersion'`.

- [ ] **Step 3a: Add the entities**

Add to `app/models/entities.py` (after `ReviewLearningSetting`), and add `ShadowDecision`
to the `from app.models.enums import (...)` block:

```python
class LearningModelVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "learning_model_versions"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "segment_key", "trained_at", name="uq_learning_model_versions_identity"
        ),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    segment_key: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_spec_version: Mapped[str] = mapped_column(String(32), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    n_labels: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    n_approved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    n_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cv_auc: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cv_logloss: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cv_ece: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    trained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class LearningShadowOutcome(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "learning_shadow_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "application_id", "model_version_id", name="uq_learning_shadow_outcomes_identity"
        ),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    application_id: Mapped[UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    model_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("learning_model_versions.id", ondelete="SET NULL")
    )
    segment_key: Mapped[str] = mapped_column(String(64), nullable=False)
    p_approve: Mapped[float] = mapped_column(Float, nullable=False)
    ci_low: Mapped[float] = mapped_column(Float, nullable=False)
    ci_high: Mapped[float] = mapped_column(Float, nullable=False)
    support_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    would_decide: Mapped[ShadowDecision] = mapped_column(
        enum_column(ShadowDecision), nullable=False
    )
    human_decision: Mapped[ReviewOutcome | None] = mapped_column(enum_column(ReviewOutcome))
    human_reason: Mapped[ReviewReason | None] = mapped_column(enum_column(ReviewReason))
    agreed: Mapped[bool | None] = mapped_column(Boolean)
    sampled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
```

Then add both to `app/models/__init__.py` imports and `__all__` (alphabetical:
`LearningModelVersion`, `LearningShadowOutcome` after `JobSource`).

- [ ] **Step 3b: Generate and hand-check the migration**

Run: `uv run alembic revision --autogenerate -m "learning model and shadow"`
Then open the new file in `migrations/versions/`, confirm it creates exactly the two
tables + indexes + unique constraints above and **nothing else** (no drift from other
models). Set `down_revision` to the current head (`f068404`'s migration —
verify with `uv run alembic heads`). The `downgrade()` drops both tables.

- [ ] **Step 4: Verify**

Run: `uv run alembic check && uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head && uv run pytest tests/integration/test_learning_training_and_shadow.py -v && uv run mypy app/models`
Expected: `alembic check` reports "No new upgrade operations detected"; up/down/up clean;
test PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/models/ migrations/versions/ tests/integration/test_learning_training_and_shadow.py
git commit -m "feat: add learning model version and shadow outcome tables"
```

---

## Task 11: Training orchestration

**Files:**
- Create: `app/learning/training.py`
- Modify: `app/learning/__init__.py`
- Test: `tests/integration/test_learning_training_and_shadow.py`

**Interfaces:**
- Consumes: `build_feature_spec`, `build_matrix`, `MODEL_MIN_LABELS`,
  `MODEL_MIN_PER_OUTCOME`, `FEATURE_SPEC_VERSION`, `contribution_labels` (features);
  `build_trained_model` (model); `LearningModelVersion` (entities);
  `ProfileService` (`app.profiles`).
- Produces:
  - `async def train_profile(session: AsyncSession, profile_id: UUID) -> LearningModelVersion | None`
    — loads all `ReviewFeedbackEvent` for the profile; returns `None` (and writes
    nothing) when fewer than `MODEL_MIN_LABELS` usable rows or either outcome below
    `MODEL_MIN_PER_OUTCOME`; otherwise builds the model and inserts one
    `LearningModelVersion` with `segment_key="global"`, `algorithm="l2_logistic_isotonic"`,
    `payload=model.to_json()`. Does **not** commit.
  - `async def train_all_profiles() -> int` — opens its own session via
    `async_session_factory`, iterates `ProfileService().list_profiles`, calls
    `train_profile`, commits once, returns the count of models written.
  - `async def latest_model(session: AsyncSession, profile_id: UUID, *, segment_key: str = "global") -> TrainedModel | None`
    — most recent `LearningModelVersion` for the pair, rehydrated via
    `TrainedModel.from_json`; `None` when absent.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/integration/test_learning_training_and_shadow.py
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.learning.training import latest_model, train_profile
from app.models.entities import ReviewFeedbackEvent, UserProfile
from app.models.enums import ReviewOutcome, ReviewReason


def _feedback(profile_id, outcome, category, day) -> ReviewFeedbackEvent:
    return ReviewFeedbackEvent(
        profile_id=profile_id,
        application_id=uuid4(),
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        outcome=outcome,
        reason_code=None if outcome == ReviewOutcome.APPROVED else ReviewReason.ROLE,
        actor="test",
        learning_eligible=True,
        feature_schema_version="review-v2",
        feature_snapshot={
            "features": {
                "category": [category],
                "title": ["picker" if category == "warehouses" else "agent"],
            },
            "learning_dimensions": ["category", "title"],
            "numeric": {"overall_fit": 70.0 if category == "warehouses" else 30.0},
        },
        created_at=datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=day),
    )


async def test_train_profile_needs_enough_balanced_labels(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="p", is_default=True)
        session.add(profile)
        await session.flush()
        session.add_all(
            [_feedback(profile.id, ReviewOutcome.APPROVED, "warehouses", d) for d in range(5)]
        )
        await session.flush()

        assert await train_profile(session, profile.id) is None


async def test_train_profile_writes_a_usable_model(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="p", is_default=True)
        session.add(profile)
        await session.flush()
        events = []
        for d in range(30):
            events.append(_feedback(profile.id, ReviewOutcome.APPROVED, "warehouses", d))
        for d in range(30, 55):
            events.append(_feedback(profile.id, ReviewOutcome.REJECTED, "sales", d))
        session.add_all(events)
        await session.flush()

        version = await train_profile(session, profile.id)
        assert version is not None
        assert version.n_labels == 55
        assert version.segment_key == "global"

        model = await latest_model(session, profile.id)
        assert model is not None
        assert model.feature_spec_version == "features-v3"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_learning_training_and_shadow.py -k train -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# app/learning/training.py
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory
from app.learning.features import (
    FEATURE_SPEC_VERSION,
    MODEL_MIN_LABELS,
    MODEL_MIN_PER_OUTCOME,
    build_feature_spec,
    build_matrix,
)
from app.learning.model import TrainedModel, build_trained_model
from app.models.entities import LearningModelVersion, ReviewFeedbackEvent
from app.profiles import ProfileService

GLOBAL_SEGMENT = "global"
ALGORITHM = "l2_logistic_isotonic"


async def _load_events(session: AsyncSession, profile_id: UUID) -> list[ReviewFeedbackEvent]:
    return list(
        (
            await session.scalars(
                select(ReviewFeedbackEvent)
                .where(ReviewFeedbackEvent.profile_id == profile_id)
                .order_by(ReviewFeedbackEvent.created_at)
            )
        ).all()
    )


async def train_profile(session: AsyncSession, profile_id: UUID) -> LearningModelVersion | None:
    events = await _load_events(session, profile_id)
    spec = build_feature_spec(events)
    x, y, w, frequencies = build_matrix(events, spec)
    if len(y) < MODEL_MIN_LABELS:
        return None
    if float(y.sum()) < MODEL_MIN_PER_OUTCOME or float((1.0 - y).sum()) < MODEL_MIN_PER_OUTCOME:
        return None
    model = build_trained_model(
        feature_spec=spec.to_dict(),
        feature_spec_version=FEATURE_SPEC_VERSION,
        feature_names=spec.feature_names(),
        x=x,
        y=y,
        w=w,
        feature_frequencies=frequencies,
    )
    version = LearningModelVersion(
        profile_id=profile_id,
        segment_key=GLOBAL_SEGMENT,
        feature_spec_version=FEATURE_SPEC_VERSION,
        algorithm=ALGORITHM,
        payload=model.to_json(),
        n_labels=model.n_labels,
        n_approved=model.n_approved,
        n_rejected=model.n_rejected,
        cv_auc=model.cv_auc,
        cv_logloss=model.cv_logloss,
        cv_ece=model.cv_ece,
    )
    session.add(version)
    await session.flush()
    return version


async def train_all_profiles() -> int:
    written = 0
    async with async_session_factory() as session:
        for profile in await ProfileService().list_profiles(session):
            if await train_profile(session, profile.id) is not None:
                written += 1
        await session.commit()
    return written


async def latest_model(
    session: AsyncSession, profile_id: UUID, *, segment_key: str = GLOBAL_SEGMENT
) -> TrainedModel | None:
    version = await session.scalar(
        select(LearningModelVersion)
        .where(
            LearningModelVersion.profile_id == profile_id,
            LearningModelVersion.segment_key == segment_key,
        )
        .order_by(LearningModelVersion.trained_at.desc())
        .limit(1)
    )
    if version is None:
        return None
    return TrainedModel.from_json(version.payload)
```

Add to `app/learning/__init__.py`: import and export `train_all_profiles`,
`train_profile`, `latest_model`, `GLOBAL_SEGMENT`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_learning_training_and_shadow.py -v && uv run mypy app/learning`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/learning/training.py app/learning/__init__.py tests/integration/test_learning_training_and_shadow.py
git commit -m "feat: train and persist per-profile review-learning models"
```

---

## Task 12: Enrich `record_decision` snapshot with v3 extras

**Files:**
- Modify: `app/learning/service.py`
- Test: `tests/unit/test_review_learning.py` is **not** modified; add cases to
  `tests/integration/test_learning_training_and_shadow.py`.

**Interfaces:**
- Consumes: `build_snapshot_extras` (features); `JobPreference` model.
- Produces: after this task every new `ReviewFeedbackEvent.feature_snapshot` also has
  `"numeric"` and `"context"` keys. `feature_schema_version` is still `"review-v2"`.
  The existing `snapshot` dict in `record_decision` gains `**build_snapshot_extras(...)`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/integration/test_learning_training_and_shadow.py
from app.learning.service import ReviewLearningService
from app.models.entities import (
    Application,
    CanonicalJob,
    EmployerContact,
    JobPreference,
    MatchEvaluation,
    Resume,
    SourceJob,
)
from app.models.enums import ApplicationStatus, ContactType, MatchDecision, VerificationStatus


async def _prepared_application(session) -> Application:
    profile = UserProfile(name="p", is_default=True)
    session.add(profile)
    await session.flush()
    session.add(JobPreference(profile_id=profile.id, minimum_salary=10000))
    canonical = CanonicalJob(
        normalized_company="c", normalized_title="t", canonical_fingerprint=uuid4().hex
    )
    session.add(canonical)
    await session.flush()
    job = SourceJob(
        source_id=uuid4(),
        canonical_job_id=canonical.id,
        external_job_id="x",
        canonical_url="https://e/j",
        title="Picker",
        content_hash="a",
        matching_content_hash="b",
        source_fingerprint="c",
        salary_min=8000,
        raw_metadata={"adapter_type": "rabota_md"},
    )
    resume = Resume(
        profile_id=profile.id,
        name="r",
        category="logistics",
        storage_key=uuid4().hex,
        original_filename="r.pdf",
        mime_type="application/pdf",
        sha256="d" * 64,
        active=True,
        verified=True,
    )
    session.add_all([job, resume])
    await session.flush()
    evaluation = MatchEvaluation(
        profile_id=profile.id,
        canonical_job_id=canonical.id,
        source_job_id=job.id,
        resume_fit=60,
        preference_fit=80,
        overall_fit=72,
        requirements_met=[],
        missing_requirements=[],
        risks=[],
        scam_indicators=[],
        explanation="",
        decision=MatchDecision.PREPARE_FOR_REVIEW,
        model="m",
        prompt_rules_version="v",
        source_content_hash="a",
        source_matching_hash="b",
        resume_id=resume.id,
        resume_sha256="d" * 64,
    )
    contact = EmployerContact(
        canonical_job_id=canonical.id,
        source_job_id=job.id,
        value="hr@e.test",
        contact_type=ContactType.EMAIL,
        discovery_source="page",
        verification_status=VerificationStatus.VERIFIED,
        evidence_url="https://e/j",
    )
    session.add_all([evaluation, contact])
    await session.flush()
    application = Application(
        profile_id=profile.id,
        canonical_job_id=canonical.id,
        source_job_id=job.id,
        match_evaluation_id=evaluation.id,
        resume_id=resume.id,
        recipient_contact_id=contact.id,
        subject="Отклик на вакансию «Picker»",
        body="body",
        language="ru",
        status=ApplicationStatus.PENDING_REVIEW,
        idempotency_key=uuid4().hex,
        content_validated=True,
    )
    session.add(application)
    await session.flush()
    return application


async def test_record_decision_stores_numeric_and_context(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        application = await _prepared_application(session)
        event = await ReviewLearningService().record_decision(
            session, application, outcome=ReviewOutcome.APPROVED, actor="test"
        )
        assert event.feature_schema_version == "review-v2"
        assert event.feature_snapshot["numeric"]["overall_fit"] == 0.72
        assert event.feature_snapshot["numeric"]["salary_gap"] == -0.2
        assert event.feature_snapshot["context"]["source_key"] == "rabota_md"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_learning_training_and_shadow.py -k record_decision -v`
Expected: FAIL — `KeyError: 'numeric'`.

- [ ] **Step 3: Implement**

In `app/learning/service.py`, in `record_decision`, after `resume = await session.get(...)`
and before building `snapshot`, add:

```python
        from app.learning.features import build_snapshot_extras

        preference = await session.scalar(
            select(JobPreference).where(JobPreference.profile_id == application.profile_id)
        )
        snapshot_extras = build_snapshot_extras(job, evaluation, preference)
```

(add `JobPreference` to the `from app.models.entities import (...)` block.)

Then change the `snapshot` literal from:

```python
        snapshot = {
            "features": _feature_snapshot(review_job_input(job)),
            "learning_dimensions": list(dimensions),
            "context": {
                "resume_category": resume.category if resume is not None else None,
            },
        }
```

to:

```python
        snapshot = {
            "features": _feature_snapshot(review_job_input(job)),
            "learning_dimensions": list(dimensions),
            "numeric": snapshot_extras["numeric"],
            "context": {
                "resume_category": resume.category if resume is not None else None,
                **snapshot_extras["context"],
            },
        }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_review_learning.py tests/integration/test_learning_training_and_shadow.py -v && uv run mypy app/learning/service.py`
Expected: PASS (existing review-learning tests still green); mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/learning/service.py tests/integration/test_learning_training_and_shadow.py
git commit -m "feat: capture numeric and source features in review feedback snapshots"
```

---

## Task 13: Shadow-outcome recording

**Files:**
- Create: `app/learning/shadow.py`
- Modify: `app/learning/__init__.py`
- Test: `tests/integration/test_learning_training_and_shadow.py`

**Interfaces:**
- Consumes: `latest_model`, `GLOBAL_SEGMENT` (training); `extract_live`, `vectorize`,
  `present_values`, `contribution_labels`, `FeatureSpec` (features); `predict` (model);
  `LearningModelVersion`, `LearningShadowOutcome`, `Application`, `SourceJob`,
  `MatchEvaluation`, `JobPreference` (entities); `evaluation_is_current`
  (`app.matching.freshness`).
- Produces:
  - `async def record_shadow_outcomes(session: AsyncSession) -> int` — for every
    `Application` in `PENDING_REVIEW` whose bound `MatchEvaluation` is current
    (`evaluation_is_current`) and whose profile has a `latest_model`, computes a
    `Prediction` and **upserts** a `LearningShadowOutcome` keyed by
    `(application_id, model_version_id)` (where `model_version_id` is the id of the
    profile's newest `LearningModelVersion` row). Existing rows with a non-null
    `human_decision` are left untouched. Returns rows written. Does not commit.
  - `async def record_learning_shadow() -> int` — own session + commit wrapper.
- Produces: `app/learning/__init__.py` exports `record_shadow_outcomes`,
  `record_learning_shadow`.

Note: `record_shadow_outcomes` needs the `LearningModelVersion.id`. Extend `latest_model`
is avoided; instead add `async def latest_model_version(session, profile_id, *, segment_key=GLOBAL_SEGMENT) -> LearningModelVersion | None` to `training.py` in this task
and build the `TrainedModel` from its `payload`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/integration/test_learning_training_and_shadow.py
from app.learning.shadow import record_shadow_outcomes
from app.models.entities import LearningShadowOutcome


async def test_shadow_outcome_recorded_for_pending_application(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        application = await _prepared_application(session)
        # enough labels for a model, same profile
        events = []
        for d in range(30):
            events.append(
                _feedback(application.profile_id, ReviewOutcome.APPROVED, "warehouses", d)
            )
        for d in range(30, 55):
            events.append(_feedback(application.profile_id, ReviewOutcome.REJECTED, "sales", d))
        session.add_all(events)
        await session.flush()
        await train_profile(session, application.profile_id)
        await session.flush()

        written = await record_shadow_outcomes(session)
        await session.flush()

        assert written == 1
        outcome = (await session.scalars(select(LearningShadowOutcome))).one()
        assert outcome.application_id == application.id
        assert 0.0 <= outcome.p_approve <= 1.0
        assert outcome.human_decision is None

        # idempotent: no second row, no overwrite
        assert await record_shadow_outcomes(session) == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_learning_training_and_shadow.py -k shadow_outcome_recorded -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# app/learning/training.py — append
async def latest_model_version(
    session: AsyncSession, profile_id: UUID, *, segment_key: str = GLOBAL_SEGMENT
) -> LearningModelVersion | None:
    return await session.scalar(
        select(LearningModelVersion)
        .where(
            LearningModelVersion.profile_id == profile_id,
            LearningModelVersion.segment_key == segment_key,
        )
        .order_by(LearningModelVersion.trained_at.desc())
        .limit(1)
    )
```

```python
# app/learning/shadow.py
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory
from app.learning.features import (
    FeatureSpec,
    contribution_labels,
    extract_live,
    present_values,
    vectorize,
)
from app.learning.model import TrainedModel, predict
from app.learning.training import GLOBAL_SEGMENT, latest_model_version
from app.matching.freshness import evaluation_is_current
from app.models.entities import (
    Application,
    JobPreference,
    LearningModelVersion,
    LearningShadowOutcome,
    MatchEvaluation,
    SourceJob,
)
from app.models.enums import ApplicationStatus


async def record_shadow_outcomes(session: AsyncSession) -> int:
    rows = (
        await session.execute(
            select(Application, SourceJob, MatchEvaluation)
            .join(SourceJob, SourceJob.id == Application.source_job_id)
            .join(MatchEvaluation, MatchEvaluation.id == Application.match_evaluation_id)
            .where(Application.status == ApplicationStatus.PENDING_REVIEW)
        )
    ).all()
    if not rows:
        return 0
    models: dict[str, tuple[LearningModelVersion, TrainedModel, FeatureSpec]] = {}
    preferences: dict[str, JobPreference | None] = {}
    written = 0
    for application, job, evaluation in rows:
        key = str(application.profile_id)
        if key not in models:
            version = await latest_model_version(session, application.profile_id)
            if version is None:
                models[key] = None  # type: ignore[assignment]
            else:
                trained = TrainedModel.from_json(version.payload)
                models[key] = (version, trained, FeatureSpec.from_dict(trained.feature_spec))
        entry = models[key]
        if entry is None:
            continue
        version, trained, spec = entry
        existing = await session.scalar(
            select(LearningShadowOutcome).where(
                LearningShadowOutcome.application_id == application.id,
                LearningShadowOutcome.model_version_id == version.id,
            )
        )
        if existing is not None:
            continue
        if not await evaluation_is_current(session, evaluation, job):
            continue
        if key not in preferences:
            preferences[key] = await session.scalar(
                select(JobPreference).where(JobPreference.profile_id == application.profile_id)
            )
        preference = preferences[key]
        if preference is None:
            continue
        features = extract_live(job, evaluation, preference)
        prediction = predict(
            trained,
            row=vectorize(spec, features),
            present_values=present_values(spec, features),
            contribution_labels=contribution_labels(spec),
        )
        session.add(
            LearningShadowOutcome(
                profile_id=application.profile_id,
                application_id=application.id,
                model_version_id=version.id,
                segment_key=GLOBAL_SEGMENT,
                p_approve=prediction.p_approve,
                ci_low=prediction.ci_low,
                ci_high=prediction.ci_high,
                support_ok=prediction.support_ok,
                would_decide=prediction.would_decide,
                sampled=False,
            )
        )
        written += 1
    return written


async def record_learning_shadow() -> int:
    async with async_session_factory() as session:
        written = await record_shadow_outcomes(session)
        await session.commit()
        return written
```

Add exports to `app/learning/__init__.py`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_learning_training_and_shadow.py -v && uv run mypy app/learning`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/learning/shadow.py app/learning/training.py app/learning/__init__.py tests/integration/test_learning_training_and_shadow.py
git commit -m "feat: record shadow-mode model predictions for pending reviews"
```

---

## Task 14: Resolve shadow outcomes against the operator's decision

**Files:**
- Modify: `app/learning/shadow.py`, `app/learning/service.py`
- Test: `tests/unit/test_learning_shadow.py`, `tests/integration/test_learning_training_and_shadow.py`

**Interfaces:**
- Consumes: `LearningShadowOutcome`, `ShadowDecision`, `ReviewOutcome`, `ReviewReason`.
- Produces:
  - `async def attach_human_decision(session: AsyncSession, application_id: UUID, outcome: ReviewOutcome, reason: ReviewReason | None) -> int`
    — for every `LearningShadowOutcome` of that application with `human_decision IS NULL`,
    sets `human_decision`, `human_reason`, and `agreed` where:
    `agreed = True` if `would_decide == APPROVE and outcome == APPROVED`
    or `would_decide == REJECT and outcome == REJECTED`;
    `agreed = False` if `would_decide` is `APPROVE`/`REJECT` and disagrees;
    `agreed = None` if `would_decide == ABSTAIN`. Returns rows updated. No commit.
  - `agreement_of(would_decide: ShadowDecision, outcome: ReviewOutcome) -> bool | None`
    (pure helper, tested directly).
- Wiring: `ReviewLearningService.record_decision`, right before `return event`, calls
  `await attach_human_decision(session, application.id, outcome, reason)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_learning_shadow.py
from app.learning.shadow import agreement_of
from app.models.enums import ReviewOutcome, ShadowDecision


def test_agreement_matrix() -> None:
    assert agreement_of(ShadowDecision.APPROVE, ReviewOutcome.APPROVED) is True
    assert agreement_of(ShadowDecision.APPROVE, ReviewOutcome.REJECTED) is False
    assert agreement_of(ShadowDecision.REJECT, ReviewOutcome.REJECTED) is True
    assert agreement_of(ShadowDecision.REJECT, ReviewOutcome.APPROVED) is False
    assert agreement_of(ShadowDecision.ABSTAIN, ReviewOutcome.APPROVED) is None
```

```python
# add to tests/integration/test_learning_training_and_shadow.py
async def test_human_decision_backfills_shadow_agreement(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        application = await _prepared_application(session)
        session.add(
            LearningShadowOutcome(
                profile_id=application.profile_id,
                application_id=application.id,
                model_version_id=None,
                segment_key="global",
                p_approve=0.95,
                ci_low=0.9,
                ci_high=0.98,
                support_ok=True,
                would_decide=__import__(
                    "app.models.enums", fromlist=["ShadowDecision"]
                ).ShadowDecision.APPROVE,
            )
        )
        await session.flush()

        await ReviewLearningService().record_decision(
            session, application, outcome=ReviewOutcome.APPROVED, actor="test"
        )
        await session.flush()

        row = (await session.scalars(select(LearningShadowOutcome))).one()
        assert row.human_decision == ReviewOutcome.APPROVED
        assert row.agreed is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_learning_shadow.py tests/integration/test_learning_training_and_shadow.py -k "agreement or human_decision" -v`
Expected: FAIL — `agreement_of` missing.

- [ ] **Step 3: Implement**

```python
# app/learning/shadow.py — append
from uuid import UUID

from app.models.enums import ReviewOutcome, ReviewReason, ShadowDecision


def agreement_of(would_decide: ShadowDecision, outcome: ReviewOutcome) -> bool | None:
    if would_decide is ShadowDecision.ABSTAIN:
        return None
    approved = outcome is ReviewOutcome.APPROVED
    if would_decide is ShadowDecision.APPROVE:
        return approved
    return not approved


async def attach_human_decision(
    session: AsyncSession,
    application_id: UUID,
    outcome: ReviewOutcome,
    reason: ReviewReason | None,
) -> int:
    pending = list(
        (
            await session.scalars(
                select(LearningShadowOutcome).where(
                    LearningShadowOutcome.application_id == application_id,
                    LearningShadowOutcome.human_decision.is_(None),
                )
            )
        ).all()
    )
    for row in pending:
        row.human_decision = outcome
        row.human_reason = reason
        row.agreed = agreement_of(row.would_decide, outcome)
    return len(pending)
```

In `app/learning/service.py`, `record_decision`, immediately before `return event`:

```python
        from app.learning.shadow import attach_human_decision

        await attach_human_decision(session, application.id, outcome, reason)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_learning_shadow.py tests/integration/test_learning_training_and_shadow.py tests/unit/test_review_learning.py -v && uv run mypy app/learning`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/learning/shadow.py app/learning/service.py tests/unit/test_learning_shadow.py tests/integration/test_learning_training_and_shadow.py
git commit -m "feat: resolve shadow outcomes against operator review decisions"
```

---

## Task 15: Shadow scorecard aggregation

**Files:**
- Modify: `app/learning/shadow.py`, `app/learning/__init__.py`
- Test: `tests/unit/test_learning_shadow.py`

**Interfaces:**
- Consumes: `LearningShadowOutcome`, `LearningModelVersion`, `ShadowDecision`.
- Produces:
  - `async def shadow_scorecard(session: AsyncSession, profile_id: UUID, *, window_days: int = 90) -> dict[str, Any]`
    — aggregates `LearningShadowOutcome` for the profile with
    `created_at >= now - window_days`:
    ```python
    {
        "profile_id": str,
        "window_days": int,
        "cases_total": int,
        "resolved": int,
        "would_approve": int,
        "would_reject": int,
        "would_abstain": int,
        "agreement_overall": float | None,  # over resolved non-abstain rows
        "would_approve_agreement": float | None,
        "would_reject_agreement": float | None,
        "support_ok_rate": float | None,
        "model": {  # from newest LearningModelVersion or None
            "trained_at": str,
            "n_labels": int,
            "cv_auc": float,
            "cv_logloss": float,
            "cv_ece": float,
        }
        | None,
    }
    ```
  - exported from `app/learning/__init__.py`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_learning_shadow.py
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.learning.shadow import shadow_scorecard
from app.models.entities import LearningShadowOutcome, UserProfile
from app.models.enums import ReviewOutcome, ShadowDecision

pytestmark = pytest.mark.asyncio


async def test_scorecard_counts_and_agreement(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="p", is_default=True)
        session.add(profile)
        await session.flush()

        def row(decide, human, agreed, support=True):
            return LearningShadowOutcome(
                profile_id=profile.id,
                application_id=uuid4(),
                model_version_id=None,
                segment_key="global",
                p_approve=0.5,
                ci_low=0.4,
                ci_high=0.6,
                support_ok=support,
                would_decide=decide,
                human_decision=human,
                agreed=agreed,
            )

        session.add_all(
            [
                row(ShadowDecision.APPROVE, ReviewOutcome.APPROVED, True),
                row(ShadowDecision.APPROVE, ReviewOutcome.REJECTED, False),
                row(ShadowDecision.REJECT, ReviewOutcome.REJECTED, True),
                row(ShadowDecision.ABSTAIN, None, None, support=False),
            ]
        )
        await session.flush()

        card = await shadow_scorecard(session, profile.id)

        assert card["cases_total"] == 4
        assert card["resolved"] == 3
        assert card["would_approve"] == 2
        assert card["agreement_overall"] == pytest.approx(2 / 3)
        assert card["would_approve_agreement"] == pytest.approx(0.5)
        assert card["support_ok_rate"] == pytest.approx(0.75)
        assert card["model"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_learning_shadow.py -k scorecard -v`
Expected: FAIL — `shadow_scorecard` missing.

- [ ] **Step 3: Implement**

```python
# app/learning/shadow.py — append
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models.entities import LearningModelVersion


def _rate(numer: int, denom: int) -> float | None:
    return numer / denom if denom else None


async def shadow_scorecard(
    session: AsyncSession, profile_id: UUID, *, window_days: int = 90
) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    rows = list(
        (
            await session.scalars(
                select(LearningShadowOutcome).where(
                    LearningShadowOutcome.profile_id == profile_id,
                    LearningShadowOutcome.created_at >= cutoff,
                )
            )
        ).all()
    )
    resolved = [r for r in rows if r.human_decision is not None and r.agreed is not None]
    would_approve = [r for r in resolved if r.would_decide is ShadowDecision.APPROVE]
    would_reject = [r for r in resolved if r.would_decide is ShadowDecision.REJECT]
    version = await session.scalar(
        select(LearningModelVersion)
        .where(LearningModelVersion.profile_id == profile_id)
        .order_by(LearningModelVersion.trained_at.desc())
        .limit(1)
    )
    return {
        "profile_id": str(profile_id),
        "window_days": window_days,
        "cases_total": len(rows),
        "resolved": len(resolved),
        "would_approve": sum(r.would_decide is ShadowDecision.APPROVE for r in rows),
        "would_reject": sum(r.would_decide is ShadowDecision.REJECT for r in rows),
        "would_abstain": sum(r.would_decide is ShadowDecision.ABSTAIN for r in rows),
        "agreement_overall": _rate(sum(r.agreed for r in resolved), len(resolved)),
        "would_approve_agreement": _rate(sum(r.agreed for r in would_approve), len(would_approve)),
        "would_reject_agreement": _rate(sum(r.agreed for r in would_reject), len(would_reject)),
        "support_ok_rate": _rate(sum(r.support_ok for r in rows), len(rows)),
        "model": None
        if version is None
        else {
            "trained_at": version.trained_at.isoformat(),
            "n_labels": version.n_labels,
            "cv_auc": version.cv_auc,
            "cv_logloss": version.cv_logloss,
            "cv_ece": version.cv_ece,
        },
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_learning_shadow.py -v && uv run mypy app/learning`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/learning/shadow.py app/learning/__init__.py tests/unit/test_learning_shadow.py
git commit -m "feat: aggregate a shadow-mode scorecard per profile"
```

---

## Task 16: Celery tasks and schedule

**Files:**
- Modify: `app/scheduler/tasks.py`, `app/scheduler/celery_app.py`
- Test: `tests/unit/test_scheduler_learning.py` (create)

**Interfaces:**
- Consumes: `train_all_profiles` (`app.learning.training`), `record_learning_shadow`
  (`app.learning.shadow`), `_run_locked_periodic` (existing in `tasks.py`).
- Produces two Celery tasks:
  - `train_learning_models_task` — name `job_agent.scheduler.train_learning_models`,
    queue `matching`, wraps `train_all_profiles()` in `_run_locked_periodic("train-learning-models", ..., ttl_seconds=1800)`.
  - `record_learning_shadow_task` — name `job_agent.scheduler.record_learning_shadow`,
    queue `matching`, wraps `record_learning_shadow()` in
    `_run_locked_periodic("record-learning-shadow", ..., ttl_seconds=600)`.
- Produces `beat_schedule` entries:
  - `"train-learning-models"`: `crontab(minute=30, hour=1)`, queue `matching`,
    `expires` 3000.
  - `"record-learning-shadow"`: `300.0`, queue `matching`, `expires` 270.
- Produces `task_routes` entries mapping both names to `{"queue": "matching"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_scheduler_learning.py
from app.scheduler.celery_app import celery_app
from app.scheduler.tasks import record_learning_shadow_task, train_learning_models_task


def test_learning_tasks_are_registered_and_scheduled() -> None:
    assert train_learning_models_task.name == "job_agent.scheduler.train_learning_models"
    assert record_learning_shadow_task.name == "job_agent.scheduler.record_learning_shadow"
    schedule = celery_app.conf.beat_schedule
    assert schedule["train-learning-models"]["task"] == train_learning_models_task.name
    assert schedule["record-learning-shadow"]["schedule"] == 300.0
    routes = celery_app.conf.task_routes
    assert routes[train_learning_models_task.name] == {"queue": "matching"}
    assert routes[record_learning_shadow_task.name] == {"queue": "matching"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_scheduler_learning.py -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

Append to `app/scheduler/tasks.py` (before `__all__`, then add both names to `__all__`):

```python
@celery_app.task(name="job_agent.scheduler.train_learning_models")
def train_learning_models_task() -> int | dict[str, str]:
    from app.learning.training import train_all_profiles

    return _run_locked_periodic("train-learning-models", train_all_profiles(), ttl_seconds=1800)


@celery_app.task(name="job_agent.scheduler.record_learning_shadow")
def record_learning_shadow_task() -> int | dict[str, str]:
    from app.learning.shadow import record_learning_shadow

    return _run_locked_periodic("record-learning-shadow", record_learning_shadow(), ttl_seconds=600)
```

In `app/scheduler/celery_app.py`, add to `beat_schedule`:

```python
        "train-learning-models": {
            "task": "job_agent.scheduler.train_learning_models",
            "schedule": crontab(minute=30, hour=1),
            "options": {"queue": "matching", "expires": 3000},
        },
        "record-learning-shadow": {
            "task": "job_agent.scheduler.record_learning_shadow",
            "schedule": 300.0,
            "options": {"queue": "matching", "expires": 270},
        },
```

and to `task_routes`:

```python
        "job_agent.scheduler.train_learning_models": {"queue": "matching"},
        "job_agent.scheduler.record_learning_shadow": {"queue": "matching"},
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_scheduler_learning.py tests/unit/test_observability.py -v && uv run mypy app/scheduler`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/scheduler/ tests/unit/test_scheduler_learning.py
git commit -m "feat: schedule review-learning training and shadow recording"
```

---

## Task 17: Daily-report shadow block

**Files:**
- Modify: `app/reports/service.py`
- Test: `tests/unit/test_reports.py`

**Interfaces:**
- Consumes: `shadow_scorecard` (`app.learning.shadow`), `ProfileService`.
- Produces: the daily `summary` dict gains
  `"learning_shadow": list[dict]` — one `shadow_scorecard(session, profile.id)` per
  profile returned by `ProfileService().list_profiles(session)`. Empty list when there
  are no profiles.

- [ ] **Step 1: Write the failing test**

Inspect `tests/unit/test_reports.py` for the existing generate-report test and its
fixtures; add:

```python
async def test_daily_report_includes_learning_shadow_block(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        profile = UserProfile(name="p", is_default=True)
        session.add(profile)
        await session.flush()

        report = await _generate(session)  # import from app.reports.service

    assert "learning_shadow" in report.summary
    assert isinstance(report.summary["learning_shadow"], list)
    assert report.summary["learning_shadow"][0]["profile_id"] == str(profile.id)
```

(Match the import style already used in `tests/unit/test_reports.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_reports.py -k learning_shadow -v`
Expected: FAIL — `KeyError: 'learning_shadow'`.

- [ ] **Step 3: Implement**

In `app/reports/service.py`, inside `_generate`, after the `summary = {...}` dict is
built and before the `existing = await session.scalar(...)` lookup, add:

```python
    from app.learning.shadow import shadow_scorecard
    from app.profiles import ProfileService

    summary["learning_shadow"] = [
        await shadow_scorecard(session, profile.id)
        for profile in await ProfileService().list_profiles(session)
    ]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_reports.py -v && uv run mypy app/reports`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/reports/service.py tests/unit/test_reports.py
git commit -m "feat: surface the learning shadow scorecard in the daily report"
```

---

## Task 18: Read-only MCP status tool

**Files:**
- Modify: `app/mcp/server.py`
- Test: `tests/integration/test_interfaces.py` (add a case following the file's pattern)

**Interfaces:**
- Consumes: `shadow_scorecard` (`app.learning.shadow`), `latest_model_version`
  (`app.learning.training`), `ProfileService`, `async_session_factory`.
- Produces MCP tool `get_learning_model_status(profile_id: str | None = None) -> dict[str, Any]`
  returning:
  ```python
  {
    "profile_id": str,
    "segment_key": "global",
    "model": {  # or None
      "trained_at": str, "feature_spec_version": str, "algorithm": str,
      "n_labels": int, "n_approved": int, "n_rejected": int,
      "cv_auc": float, "cv_logloss": float, "cv_ece": float,
    },
    "shadow": { ...shadow_scorecard()... },
  }
  ```
  Read-only, no audit event, mirrors `get_review_learning_status` structure.

- [ ] **Step 1: Write the failing test**

Follow the existing MCP tool test pattern in `tests/integration/test_interfaces.py`
(it drives tools through the in-process MCP app). Add:

```python
async def test_get_learning_model_status_reports_no_model_initially(mcp_client) -> None:
    result = await mcp_client.call_tool("get_learning_model_status", {})
    payload = result.structured_content
    assert payload["segment_key"] == "global"
    assert payload["model"] is None
    assert payload["shadow"]["cases_total"] == 0
```

(Use whatever helper/fixture name the file already uses for an authenticated MCP call —
match it exactly; do not invent `mcp_client` if the file calls it something else.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_interfaces.py -k learning_model_status -v`
Expected: FAIL — unknown tool.

- [ ] **Step 3: Implement**

In `app/mcp/server.py`, next to `get_review_learning_status` (~line 1248):

```python
@mcp.tool()
async def get_learning_model_status(profile_id: str | None = None) -> dict[str, Any]:
    """Report the latest calibrated learning model and its shadow-mode scorecard."""
    from app.database.session import async_session_factory
    from app.learning.shadow import shadow_scorecard
    from app.learning.training import latest_model_version

    async with async_session_factory() as session:
        profile = await ProfileService().get_profile(
            session, UUID(profile_id) if profile_id else None
        )
        if profile is None:
            raise ValueError("profile not found")
        version = await latest_model_version(session, profile.id)
        return {
            "profile_id": str(profile.id),
            "segment_key": "global",
            "model": None
            if version is None
            else {
                "trained_at": version.trained_at.isoformat(),
                "feature_spec_version": version.feature_spec_version,
                "algorithm": version.algorithm,
                "n_labels": version.n_labels,
                "n_approved": version.n_approved,
                "n_rejected": version.n_rejected,
                "cv_auc": version.cv_auc,
                "cv_logloss": version.cv_logloss,
                "cv_ece": version.cv_ece,
            },
            "shadow": await shadow_scorecard(session, profile.id),
        }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_interfaces.py -v && uv run mypy app/mcp`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add app/mcp/server.py tests/integration/test_interfaces.py
git commit -m "feat: add read-only get_learning_model_status MCP tool"
```

---

## Task 19: End-to-end integration test, docs, full verification

**Files:**
- Modify: `tests/integration/test_learning_training_and_shadow.py`, `docs/review-learning.md`

**Interfaces:**
- Consumes: everything above.
- Produces: one end-to-end test proving the loop; a documentation section; a green
  `./scripts/verify.sh` + `alembic check`.

- [ ] **Step 1: Write the end-to-end test**

```python
# add to tests/integration/test_learning_training_and_shadow.py
async def test_full_shadow_loop(sqlite_session_factory) -> None:
    async with sqlite_session_factory() as session:
        application = await _prepared_application(session)
        events = []
        for d in range(30):
            events.append(
                _feedback(application.profile_id, ReviewOutcome.APPROVED, "warehouses", d)
            )
        for d in range(30, 60):
            events.append(_feedback(application.profile_id, ReviewOutcome.REJECTED, "sales", d))
        session.add_all(events)
        await session.flush()

        assert await train_profile(session, application.profile_id) is not None
        await session.flush()
        assert await record_shadow_outcomes(session) == 1
        await session.flush()

        await ReviewLearningService().record_decision(
            session, application, outcome=ReviewOutcome.APPROVED, actor="test"
        )
        await session.flush()

        card = await shadow_scorecard(session, application.profile_id)
        assert card["cases_total"] == 1
        assert card["resolved"] == 1
        assert card["model"]["n_labels"] == 60
```

- [ ] **Step 2: Run the whole new suite**

Run: `uv run pytest tests/unit/test_learning_model.py tests/unit/test_learning_features.py tests/unit/test_learning_shadow.py tests/unit/test_scheduler_learning.py tests/integration/test_learning_training_and_shadow.py -v`
Expected: PASS.

- [ ] **Step 3: Document**

Append to `docs/review-learning.md`:

```markdown
## Модель v3 и shadow-режим

Помимо счётчиков и подсказок, ночной таск `train_learning_models` строит на явных
метках владельца калиброванную интерпретируемую модель (L2-логистическая регрессия
+ изотоническая калибровка вероятности) и сохраняет её версию в
`learning_model_versions` вместе с time-series CV-метриками (AUC, log-loss, ECE).

Каждые 5 минут `record_learning_shadow` предсказывает `P(одобрю)` для заявок в
очереди «Требуют решения» и сохраняет `learning_shadow_outcomes` — что модель
сделала бы (`approve` / `reject` / `abstain`) и с какой уверенностью. Когда владелец
принимает явное решение, запись дополняется фактическим исходом и признаком
совпадения.

Shadow-режим **не влияет** на очередь, отправку или блокировку. Он собирает
доказательную базу: агрегированный scorecard виден в дневном отчёте и через MCP
`get_learning_model_status`. Автономные действия на основе этой модели —
последующие фазы (`docs/superpowers/specs/2026-08-31-learning-autonomy-design.md`).
```

- [ ] **Step 4: Full verification**

Run:
```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy app fixture_site
uv run pytest
uv run alembic check
```
Expected: all PASS. If `ruff format` flags files, run `uv run ruff format .` and
re-stage.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_learning_training_and_shadow.py docs/review-learning.md
git commit -m "test: end-to-end shadow loop; docs: learning model v3 and shadow mode"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage (Phase 1 items from spec §13):**

| Spec Phase 1 item | Task |
|---|---|
| `numpy` in deps | 1 |
| `app/learning/features.py` (v3) | 7, 8, 9 |
| `app/learning/model.py` (IRLS + PAVA + метрики) | 2, 3, 4, 5, 6 |
| `LearningModelVersion` + миграция | 10 |
| `feature_snapshot` расширяется до features-v3 in `record_decision` | 12 |
| Beat task `train_learning_models` + celery registration | 11, 16 |
| `LearningShadowOutcome` + запись из pending + backfill from `decide_review`/admin | 13, 14 |
| Daily report: shadow scorecard | 17 |
| read-only MCP `get_learning_model_status` | 18 |
| `ReviewLearningService.score()` switch to calibrated model | **Deferred to Phase 2 task 1** — see note below |
| test rework | Not needed — `test_review_learning.py` untouched (see Global Constraints) |

**Deviation from spec, deliberate:** the spec §13 lists switching
`ReviewLearningService.score()` onto the calibrated model within Phase 1. This plan
**defers** that one item to the first task of Phase 2. Rationale: it is the only
Phase 1 item that changes *existing observable behaviour* (queue ordering) and forces
a rewrite of `tests/unit/test_review_learning.py`; keeping it out makes Phase 1
provably side-effect-free and lets the shadow scorecard validate the new model
*before* it influences anything the operator sees. The spec's Phase 1 goal
("модель v3 + калибровка + shadow (автономии нет)") is fully met without it.

**2. Placeholder scan:** No "TBD"/"handle errors"/"similar to Task N". Every code step
has runnable code. Two tasks (17, 18) instruct the implementer to *match an existing
test-helper name in the target file* rather than hard-coding a fixture name that may be
wrong — this is a direction to read, not a placeholder.

**3. Type consistency:** `ShadowDecision` (enum) defined in Task 6 step 3a, used in
6/10/13/14/15. `TrainedModel.feature_spec: dict[str, Any]` (opaque) in Task 6 —
`shadow.py` rebuilds `FeatureSpec` via `FeatureSpec.from_dict(trained.feature_spec)`
(Task 13), consistent. `predict(model, *, row, present_values, contribution_labels)`
signature identical in Tasks 6, 13. `build_matrix` returns
`(X, y, w, dict[str,int])` in Task 9, consumed with that shape in Task 11.
`latest_model` vs `latest_model_version` — two distinct functions, both in
`training.py`, introduced in Tasks 11 and 13 respectively, names used consistently
afterwards. `shadow_scorecard(session, profile_id, *, window_days=90)` — same in
Tasks 15, 17, 18.

**4. Risk notes for the executor:**
- numpy + mypy strict: Task 1 adds a `warn_return_any = false` override for the three
  numeric modules. If mypy still complains inside those modules, prefer an explicit
  `float(...)` / `np.asarray(..., dtype=np.float64)` cast over a broad `# type: ignore`.
- `alembic revision --autogenerate` (Task 10) may emit `alter`/`drop` lines for
  unrelated pre-existing drift. Delete anything not in the Task 10 entity list before
  committing; `alembic check` after confirms parity.
- `tests/integration/*` need `pytestmark = pytest.mark.asyncio` or the `asyncio_mode
  = "auto"` in `pyproject.toml` (already set) — follow the existing integration files.
