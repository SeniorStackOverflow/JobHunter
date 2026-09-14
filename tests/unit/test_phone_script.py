from __future__ import annotations

# ruff: noqa: RUF001 — Russian script text is intentional.
from app.phone.script import (
    SCRIPT_CLOSING,
    SCRIPT_CLOSING_INTERRUPTED,
    SCRIPT_CLOSING_NO_RESPONSE,
    SCRIPT_CLOSING_SMS,
    SCRIPT_DETAILS_PROMPT,
    SCRIPT_FIRST_RESPONSE_RETRY,
    SCRIPT_GREETING,
)


def test_greeting_blocks_are_short_nonempty_strings() -> None:
    assert SCRIPT_GREETING == (
        "Здравствуйте, это автоматизированный помощник Андрея Гомонова. "
        "Он сейчас не может ответить лично. Подскажите, вы звоните по поводу работы?",
    )
    assert "автоматизированный помощник" in SCRIPT_GREETING[0]
    assert "часовой пояс" not in SCRIPT_GREETING[0]
    assert SCRIPT_FIRST_RESPONSE_RETRY
    assert SCRIPT_DETAILS_PROMPT
    assert SCRIPT_CLOSING_NO_RESPONSE
    for block in SCRIPT_GREETING:
        assert isinstance(block, str)
        assert 0 < len(block) <= 200


def test_closing_blocks_present() -> None:
    assert SCRIPT_CLOSING and SCRIPT_CLOSING_INTERRUPTED
    assert SCRIPT_CLOSING != SCRIPT_CLOSING_INTERRUPTED


def test_sms_closing_asks_for_interview_details() -> None:
    assert SCRIPT_CLOSING_SMS == (
        "Спасибо. Чтобы избежать ошибки в дате и времени, пожалуйста, отправьте данные "
        "собеседования SMS на этот номер. Андрей свяжется с вами. Всего доброго."
    )
