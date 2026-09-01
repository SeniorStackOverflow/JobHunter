from __future__ import annotations

import pytest

from app.phone.numbers import mask_phone, normalize_e164


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+373 60 111 222", "+37360111222"),
        ("tel:+37360111222", "+37360111222"),
        ("060111222", "+37360111222"),
        ("00373 60 111 222", "+37360111222"),
        ("  +37360111222  ", "+37360111222"),
        ("", None),
        (None, None),
        ("not a phone", None),
        ("12", None),
    ],
)
def test_normalize_e164(raw: str | None, expected: str | None) -> None:
    assert normalize_e164(raw, region="MD") == expected


def test_normalize_is_idempotent() -> None:
    once = normalize_e164("060111222", region="MD")
    assert once is not None
    assert normalize_e164(once, region="MD") == once


def test_mask_phone_keeps_country_and_tail() -> None:
    assert mask_phone("+37360111222") == "+373••••222"
    assert mask_phone("") == "(withheld)"
