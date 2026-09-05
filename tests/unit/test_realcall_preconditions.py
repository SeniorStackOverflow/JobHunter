from __future__ import annotations

from tests.realcall.a06_originate import A06Rig


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
