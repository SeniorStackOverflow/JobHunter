from __future__ import annotations

import subprocess

import httpx
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


@pytest.mark.parametrize("failure", ["ssh", "stderr", "http"])
def test_precondition_reasons_do_not_expose_transport_details(monkeypatch, failure) -> None:
    private = "private-token employer content"
    rig = A06Rig(
        "host",
        "22",
        "user",
        "",
        "",
        "",
        a14_number="fake",
        phonegate_url="http://gate",
        phonegate_token=private,
    )

    def ssh(*args, **kwargs):
        if failure == "ssh":
            raise OSError(private)
        return subprocess.CompletedProcess(
            [], 1 if failure == "stderr" else 0, stdout="", stderr=private
        )

    def get(*args, **kwargs):
        raise httpx.ConnectError(private)

    monkeypatch.setattr(rig, "_ssh", ssh)
    monkeypatch.setattr(httpx, "get", get)
    reasons = rig.check_preconditions()
    assert reasons
    assert private not in repr(reasons)


@pytest.mark.parametrize("state", ["disabled", "pending"])
def test_live_acceptance_allows_unconfigured_telegram_state(state: str) -> None:
    assert _telegram_state_allowed({"telegram": {"state": state}}, configured=False)


def test_live_acceptance_requires_sent_telegram_when_configured() -> None:
    assert _telegram_state_allowed({"telegram": {"state": "sent"}}, configured=True)
    assert not _telegram_state_allowed({"telegram": {"state": "disabled"}}, configured=True)
