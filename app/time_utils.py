from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

LOCAL_TIMEZONE_NAME = "Europe/Chisinau"
LOCAL_TZ = ZoneInfo(LOCAL_TIMEZONE_NAME)


def local_day_bounds(
    *, now: datetime | None = None, day: date | None = None
) -> tuple[datetime, datetime, datetime]:
    """Return local midnight plus UTC bounds for one Europe/Chisinau calendar day."""
    if day is None:
        current = now or datetime.now(LOCAL_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=LOCAL_TZ)
        else:
            current = current.astimezone(LOCAL_TZ)
        day = current.date()
    start_local = datetime.combine(day, time.min, LOCAL_TZ)
    end_local = datetime.combine(day + timedelta(days=1), time.min, LOCAL_TZ)
    return start_local, start_local.astimezone(UTC), end_local.astimezone(UTC)
