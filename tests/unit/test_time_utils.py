from datetime import UTC, date, datetime

from app.time_utils import LOCAL_TIMEZONE_NAME, local_day_bounds


def test_chisinau_calendar_day_uses_local_midnight_not_utc_midnight() -> None:
    start_local, start_utc, end_utc = local_day_bounds(day=date(2026, 9, 1))

    assert LOCAL_TIMEZONE_NAME == "Europe/Chisinau"
    assert start_local.isoformat() == "2026-09-01T00:00:00+03:00"
    assert start_utc == datetime(2026, 8, 31, 21, 0, tzinfo=UTC)
    assert end_utc == datetime(2026, 9, 1, 21, 0, tzinfo=UTC)
