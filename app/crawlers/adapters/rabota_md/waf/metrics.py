"""Browser-timing metrics that mimic a real challenge.js execution.

Vendored from https://github.com/Switch3301/Aws-Waf-Solver (pinned source; see waf/UPSTREAM.md,
commit fed489c54fe2eb10a6dfac5b4d4c5dfcb06b8808), verified live against
rabota.md on 2026-09-12.
"""

from __future__ import annotations

import random

COLLECTORS: list[tuple[str, str, float, float]] = [
    ("fp2", "100", 0.5, 3),
    ("browser", "101", 0, 1),
    ("capabilities", "102", 2, 8),
    ("gpu", "103", 3, 12),
    ("dnt", "104", 0, 1),
    ("math", "105", 0, 1),
    ("screen", "106", 0, 1),
    ("navigator", "107", 0, 1),
    ("auto", "108", 0, 1),
    ("stealth", "undefined", 1, 4),
    ("subtle", "110", 0, 1),
    ("canvas", "111", 80, 200),
    ("formdetector", "112", 0, 3),
    ("be", "undefined", 0, 1),
]


def _rand(lo: float, hi: float) -> float:
    return round(random.uniform(lo, hi), 1)  # noqa: S311 - non-crypto timing jitter


def build_metrics(has_token: bool = False) -> tuple[list[dict[str, object]], dict[str, int]]:
    collectors = [(name, mid, _rand(lo, hi)) for name, mid, lo, hi in COLLECTORS]
    fp_metrics = {name: int(value) for name, _, value in collectors}

    enc = _rand(0.5, 3)
    crypt = _rand(2, 8)
    coll = sum(value for _, _, value in collectors)
    acq = round(coll + enc + crypt + _rand(2, 6), 1)
    chall = _rand(2, 8)
    cookie = _rand(0.1, 1)
    total = round(acq + chall + cookie, 1)

    metrics: list[dict[str, object]] = [{"name": "2", "value": enc, "unit": "2"}]
    metrics += [{"name": mid, "value": value, "unit": "2"} for _, mid, value in collectors]
    metrics += [
        {"name": "3", "value": crypt, "unit": "2"},
        {"name": "7", "value": 1 if has_token else 0, "unit": "4"},
        {"name": "1", "value": acq, "unit": "2"},
        {"name": "4", "value": chall, "unit": "2"},
        {"name": "5", "value": cookie, "unit": "2"},
        {"name": "6", "value": total, "unit": "2"},
        {"name": "8", "value": 1, "unit": "4"},
    ]
    return metrics, fp_metrics
