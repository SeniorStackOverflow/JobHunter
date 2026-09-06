from __future__ import annotations

# ruff: noqa: RUF001 — Russian test cases are intentional.
from datetime import UTC, datetime
from typing import cast

import pytest

from app.phone.critical import (
    CriticalField,
    closing_for_transcript,
    has_confirmation_critical_markers,
    normalize_critical_value,
)
from app.phone.script import SCRIPT_CLOSING, SCRIPT_CLOSING_SMS


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        ("interview_date", "завтра", "2026-09-07"),
        ("interview_date", "послезавтра", "2026-09-08"),
        ("interview_date", "12 сентября", "2026-09-12"),
        ("interview_date", "12.09.2026", "2026-09-12"),
        ("interview_time", "в 14:30", "14:30"),
        ("interview_time", "в два часа дня", "14:00"),
        ("format", "у нас в офисе", "onsite"),
        ("format", "по видеосвязи", "remote"),
        ("address", "ул. Индепенденцей, 10", "ул. Индепенденцей, 10"),
        ("interview_time", "после обеда", None),
    ],
)
def test_normalize_critical_value(field: str, raw: str, expected: str | None) -> None:
    actual = normalize_critical_value(
        cast(CriticalField, field),
        raw,
        reference_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
    )
    assert actual == expected


@pytest.mark.parametrize(
    ("field", "raw"),
    [
        ("interview_date", "завтра, а точнее послезавтра"),
        ("interview_time", "в 10:00 или в 11:00"),
        ("format", "в офисе или по видеосвязи"),
        ("timezone", "по местному времени"),
        ("address", "адрес сообщу позже"),
    ],
)
def test_normalization_does_not_resolve_conflicting_or_ambiguous_expression(
    field: str, raw: str
) -> None:
    assert (
        normalize_critical_value(
            cast(CriticalField, field),
            raw,
            reference_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
        )
        is None
    )


@pytest.mark.parametrize(
    "texts",
    [
        ["Собеседование завтра в 10"],
        ["Встречаемся по видеосвязи"],
        ["Наш адрес: ул. Пушкина, 5"],
        ["Ссылку на встречу отправлю: https://meet.example.test/abc"],
    ],
)
def test_confirmation_markers_are_detected(texts: list[str]) -> None:
    assert has_confirmation_critical_markers(texts) is True


def test_noncritical_text_has_no_confirmation_marker() -> None:
    assert has_confirmation_critical_markers(["Позвоните Андрею позже"]) is False


def test_date_marker_selects_sms_closing() -> None:
    assert closing_for_transcript(["Собеседование завтра в 10"]) == SCRIPT_CLOSING_SMS


def test_noncritical_call_keeps_existing_closing() -> None:
    assert closing_for_transcript(["Позвоните Андрею позже"]) == SCRIPT_CLOSING
