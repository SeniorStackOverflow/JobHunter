from __future__ import annotations

import pytest

from tests.realcall.a06_originate import A06Rig
from tests.realcall.test_realcall_phase_2b import _telegram_state_allowed


def test_preconditions_return_reasons_when_ssh_fails(monkeypatch) -> None:
    def _boom(*a, **k):  # type: ignore
        raise FileNotFoundError("ssh")

    monkeypatch.setattr("subprocess.run", _boom)
    rig = A06Rig(
        ssh_host="x",
        ssh_port="1",
        ssh_user="u",
        a14_serial="",
        a06_serial="",
        a06_number="060",
        phonegate_url="http://x",
        phonegate_token="t",
    )
    reasons = rig.check_preconditions()
    assert reasons and any("ssh" in r.lower() for r in reasons)


@pytest.mark.parametrize("state", ["disabled", "pending"])
def test_live_acceptance_allows_unconfigured_telegram_state(state: str) -> None:
    assert _telegram_state_allowed({"telegram": {"state": state}}, configured=False)


def test_live_acceptance_requires_sent_telegram_when_configured() -> None:
    assert _telegram_state_allowed({"telegram": {"state": "sent"}}, configured=True)
    assert not _telegram_state_allowed({"telegram": {"state": "disabled"}}, configured=True)
