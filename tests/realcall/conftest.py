from __future__ import annotations

import os

import pytest

from tests.realcall.a06_originate import A06Rig


@pytest.fixture(scope="module")
def a06_rig() -> A06Rig:
    if os.getenv("ENABLE_REALCALL_TESTS") != "true":
        pytest.skip("real-call tests are opt-in: set ENABLE_REALCALL_TESTS=true")
    rig = A06Rig(
        ssh_host=os.getenv("REALCALL_SSH_HOST", "46.225.103.75"),
        ssh_port=os.getenv("REALCALL_SSH_PORT", "39637"),
        ssh_user=os.getenv("REALCALL_SSH_USER", "andrei"),
        a14_serial=os.getenv("REALCALL_A14_SERIAL", ""),
        a06_serial=os.getenv("REALCALL_A06_SERIAL", ""),
        a06_number=os.getenv("REALCALL_A06_NUMBER", ""),
        a14_number=os.getenv("REALCALL_A14_NUMBER", ""),
        phonegate_url=os.getenv("PHONEGATE_URL", ""),
        phonegate_token=os.getenv("PHONEGATE_AUTH_TOKEN", ""),
    )
    reasons = rig.check_preconditions()
    if reasons:
        pytest.skip("real-call preconditions not met:\n  - " + "\n  - ".join(reasons))
    return rig
