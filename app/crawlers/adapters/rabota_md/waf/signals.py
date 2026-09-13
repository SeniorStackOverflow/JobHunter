"""Static browser fingerprint profile sent with the WAF challenge solution.

Vendored from https://github.com/Switch3301/Aws-Waf-Solver (pinned source; see waf/UPSTREAM.md,
commit fed489c54fe2eb10a6dfac5b4d4c5dfcb06b8808), verified live against
rabota.md on 2026-09-12. Named ``signals`` instead of upstream ``signal`` to
avoid shadowing the stdlib module.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from pathlib import Path
from typing import Any

PLUGINS: list[dict[str, str]] = [
    {"name": "PDF Viewer", "str": "PDF Viewer "},
    {"name": "Chrome PDF Viewer", "str": "Chrome PDF Viewer "},
    {"name": "Chromium PDF Viewer", "str": "Chromium PDF Viewer "},
    {"name": "Microsoft Edge PDF Viewer", "str": "Microsoft Edge PDF Viewer "},
    {"name": "WebKit built-in PDF", "str": "WebKit built-in PDF "},
]

PLUGIN_STR = "".join(p["str"] for p in PLUGINS)
SCREEN = "1920-1080-1080-24-*-*-*"

_GPU_POOL: list[dict[str, Any]] = json.loads((Path(__file__).parent / "webgl.json").read_text())

BASE_BINS = [
    14469,
    36,
    41,
    46,
    47,
    49,
    28,
    22,
    44,
    24,
    38,
    15,
    39,
    49,
    32,
    42,
    31,
    29,
    22,
    33,
    32,
    27,
    40,
    28,
    47,
    12,
    31,
    32,
    42,
    20,
    27,
    35,
    118,
    22,
    22,
    31,
    22,
    13,
    27,
    26,
    27,
    17,
    27,
    33,
    15,
    29,
    29,
    30,
    33,
    32,
    27,
    38,
    31,
    16,
    35,
    23,
    22,
    24,
    19,
    18,
    25,
    23,
    20,
    22,
    102,
    15,
    22,
    13,
    19,
    19,
    18,
    24,
    13,
    26,
    10,
    15,
    26,
    16,
    14,
    19,
    16,
    20,
    18,
    26,
    18,
    49,
    15,
    19,
    24,
    22,
    19,
    17,
    15,
    20,
    21,
    22,
    103,
    27,
    50,
    38,
    55,
    31,
    496,
    25,
    19,
    15,
    25,
    24,
    18,
    53,
    32,
    13,
    19,
    19,
    21,
    20,
    29,
    18,
    28,
    30,
    19,
    15,
    14,
    23,
    28,
    21,
    25,
    26,
    30,
    25,
    15,
    23,
    31,
    43,
    43,
    53,
    76,
    57,
    50,
    13659,
]

MATH = {
    "tan": "-1.4214488238747245",
    "sin": "0.8178819121159085",
    "cos": "-0.5753861119575491",
}


def _rand_canvas() -> tuple[int, list[int]]:
    bins = []
    for value in BASE_BINS:
        if value > 500:
            bins.append(value + random.randint(-200, 200))  # noqa: S311 - fingerprint jitter
        elif value > 80:
            bins.append(value + random.randint(-15, 15))  # noqa: S311
        else:
            bins.append(max(1, value + random.randint(-3, 3)))  # noqa: S311
    canvas_hash = random.randint(100000000, 999999999)  # noqa: S311
    return canvas_hash, bins


def build_signal(site: str, fp_metrics: dict[str, int], ua: str) -> dict[str, Any]:
    now = int(time.time() * 1000)
    gpu = random.choice(_GPU_POOL)  # noqa: S311 - static profile pick, not crypto
    canvas_hash, canvas_bins = _rand_canvas()
    return {
        "metrics": fp_metrics,
        "start": now,
        "flashVersion": None,
        "plugins": PLUGINS,
        "dupedPlugins": f"{PLUGIN_STR}||{SCREEN}",
        "screenInfo": SCREEN,
        "referrer": "",
        "userAgent": ua,
        "location": site,
        "webDriver": False,
        "capabilities": {
            "css": {
                "textShadow": 1,
                "WebkitTextStroke": 1,
                "boxShadow": 1,
                "borderRadius": 1,
                "borderImage": 1,
                "opacity": 1,
                "transform": 1,
                "transition": 1,
            },
            "js": {
                "audio": True,
                "geolocation": True,
                "localStorage": "supported",
                "touch": False,
                "video": True,
                "webWorker": True,
            },
            "elapsed": fp_metrics["capabilities"],
        },
        "gpu": gpu,
        "dnt": None,
        "math": MATH,
        "automation": {
            "wd": {"properties": {"document": [], "window": [], "navigator": []}},
            "phantom": {"properties": {"window": []}},
        },
        "stealth": {"t1": 0, "t2": 0, "i": 1, "mte": 0, "mtd": False},
        "crypto": {
            "crypto": 1,
            "subtle": 1,
            "encrypt": True,
            "decrypt": True,
            "wrapKey": True,
            "unwrapKey": True,
            "sign": True,
            "verify": True,
            "digest": True,
            "deriveBits": True,
            "deriveKey": True,
            "getRandomValues": True,
            "randomUUID": True,
        },
        "canvas": {
            "hash": canvas_hash,
            "emailHash": None,
            "histogramBins": canvas_bins,
        },
        "formDetected": False,
        "numForms": 0,
        "numFormElements": 0,
        "be": {"si": False},
        "end": now + 1,
        "errors": [],
        "version": "2.4.0",
        "id": str(uuid.uuid4()),
    }
