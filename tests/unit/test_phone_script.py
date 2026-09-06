from __future__ import annotations

# ruff: noqa: RUF001 — Russian script text is intentional.
from app.phone.script import (
    SCRIPT_CLOSING,
    SCRIPT_CLOSING_INTERRUPTED,
    SCRIPT_CLOSING_SMS,
    SCRIPT_GREETING,
)


def test_greeting_blocks_are_short_nonempty_strings() -> None:
    assert len(SCRIPT_GREETING) >= 3
    for block in SCRIPT_GREETING:
        assert isinstance(block, str)
        assert 0 < len(block) <= 200  # short blocks keep Piper + GSM quality up


def test_closing_blocks_present() -> None:
    assert SCRIPT_CLOSING and SCRIPT_CLOSING_INTERRUPTED
    assert SCRIPT_CLOSING != SCRIPT_CLOSING_INTERRUPTED


def test_sms_closing_asks_for_interview_details() -> None:
    assert SCRIPT_CLOSING_SMS == (
        "Спасибо. Чтобы избежать ошибки в дате и времени, пожалуйста, отправьте данные "
        "собеседования SMS на этот номер. Андрей свяжется с вами. Всего доброго."
    )
