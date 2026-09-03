from __future__ import annotations

import pytest

from app.phone.policy import should_answer
from app.phone.schemas import DeviceStatus
from app.settings.config import Settings


def _status(state: str = "RINGING") -> DeviceStatus:
    return DeviceStatus(call_state=state, caller_number="+37360111222")


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, phone_auto_answer_enabled=True, **kw)


@pytest.mark.parametrize(
    ("kwargs", "expected_answer", "expected_reason"),
    [
        (
            dict(
                settings=Settings(_env_file=None),
                runtime_stopped=False,
                normalized_caller="+37360111222",
            ),
            False,
            "disabled_by_config",
        ),
        (
            dict(settings=_settings(), runtime_stopped=True, normalized_caller="+37360111222"),
            False,
            "stopped_by_operator",
        ),
        (
            dict(
                settings=_settings(phone_answer_blocklist=["+37360111222"]),
                runtime_stopped=False,
                normalized_caller="+37360111222",
            ),
            False,
            "blocklisted",
        ),
        (
            dict(settings=_settings(), runtime_stopped=False, normalized_caller="+37360111222"),
            True,
            "answer",
        ),
    ],
)
def test_should_answer_table(kwargs, expected_answer, expected_reason) -> None:
    d = should_answer(status=_status(), **kwargs)
    assert d.answer is expected_answer
    assert d.reason == expected_reason


def test_not_ringing_is_ignored() -> None:
    d = should_answer(
        status=_status("IN_CALL"),
        settings=_settings(),
        runtime_stopped=False,
        normalized_caller="+37360111222",
    )
    assert d.answer is False and d.reason == "not_ringing"


def test_unknown_caller_still_answered() -> None:
    d = should_answer(
        status=_status(), settings=_settings(), runtime_stopped=False, normalized_caller=None
    )
    assert d.answer is True and d.reason == "answer"
