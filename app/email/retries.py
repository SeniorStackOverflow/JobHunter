from __future__ import annotations

from datetime import timedelta

_RETRY_DELAYS = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=6))


def retry_delay(attempt_count: int) -> timedelta:
    return _RETRY_DELAYS[min(max(attempt_count - 1, 0), len(_RETRY_DELAYS) - 1)]
