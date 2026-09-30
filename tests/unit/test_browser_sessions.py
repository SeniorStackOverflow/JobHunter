from __future__ import annotations

import hashlib
import hmac
import time
from uuid import uuid4

import pytest
from itsdangerous import TimestampSigner

from app.security.auth import AccountSessionSigner, CsrfProtector, SessionSigner

DAY = 24 * 60 * 60
KEY = "remember-session-fixture-secret-with-more-than-32-chars"


@pytest.mark.parametrize("user", [False, True])
def test_session_renewal_keeps_open_forms_valid_and_enforces_inactivity(
    monkeypatch: pytest.MonkeyPatch,
    user: bool,
) -> None:
    clock = [int(time.time())]
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _self: clock[0])
    signer = AccountSessionSigner(KEY) if user else SessionSigner(KEY)
    token = (
        signer.issue(uuid4(), 0)
        if isinstance(signer, AccountSessionSigner)
        else signer.issue("admin")
    )
    csrf = CsrfProtector(KEY)
    form = csrf.issue(token)
    assert signer.renew(token, 30 * DAY) is None
    clock[0] += 25 * DAY
    renewed = signer.renew(token, 30 * DAY)
    assert renewed is not None and renewed != token
    assert csrf.verify(form, renewed, 3600)
    assert signer.verify(renewed, 30 * DAY) == signer.verify(token, 30 * DAY)
    assert signer.renew(renewed + "tampered", 30 * DAY) is None
    clock[0] += 31 * DAY
    assert signer.verify(renewed, 30 * DAY) is None
    assert signer.renew(renewed, 30 * DAY) is None


def test_csrf_cannot_cross_browser_sessions_or_account_versions() -> None:
    signer = AccountSessionSigner(KEY)
    account_id = uuid4()
    token = signer.issue(account_id, 0)
    csrf = CsrfProtector(KEY)
    form = csrf.issue(token)
    assert not csrf.verify(form, signer.issue(account_id, 0), 3600)
    assert not csrf.verify(form, signer.issue(account_id, 1), 3600)
    assert not csrf.verify(form, SessionSigner(KEY).issue("admin"), 3600)


def test_existing_csrf_form_is_accepted_with_its_original_cookie() -> None:
    token = SessionSigner(KEY).issue("admin")
    timestamp = str(int(time.time()))
    nonce = "existing-form"
    signature = hmac.new(
        KEY.encode(), f"{token}:{timestamp}:{nonce}".encode(), hashlib.sha256
    ).hexdigest()
    assert CsrfProtector(KEY).verify(f"{timestamp}.{nonce}.{signature}", token, 3600)
