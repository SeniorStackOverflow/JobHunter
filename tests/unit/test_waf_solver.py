from __future__ import annotations

import base64
import hashlib
import json
import time

import httpx
import pytest

from app.crawlers.adapters.rabota_md.waf import (
    AwsWafSolver,
    WafBlocked,
    WafCaptchaRequired,
    WafPowTimeout,
    WafRateLimited,
    WafScriptVersionUnknown,
    WafSolveFailed,
    WafUnsupportedChallenge,
)
from app.crawlers.adapters.rabota_md.waf.crypto import decrypt, encode, encrypt
from app.crawlers.adapters.rabota_md.waf.solver import (
    _check_zeros,
    _require_allowed_url,
    _solve_pow,
)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
SITE = "https://www.rabota.md"
CHAL_HOST = "https://abc123.deadbeef.eu-central-1.token.awswaf.com"
CHAL_URL = f"{CHAL_HOST}/abc123/part/token"
SCRIPT_URL = f"{CHAL_HOST}/abc123/part/token/challenge.js"
SCRIPT_BYTES = b"/* challenge.js fixture */"
SCRIPT_SHA256 = hashlib.sha256(SCRIPT_BYTES).hexdigest()
TOKEN = "11111111-2222-4333-8444-555555555555:AAAA:BBBB"


def challenge_page() -> str:
    return (
        "<html><head><script>window.awsWafCookieDomainList = [];\n"
        'window.gokuProps = {"key":"k","iv":"i","context":"c"};</script>\n'
        f'<script src="{SCRIPT_URL}"></script></head><body>'
        '<div id="challenge-container"></div></body></html>'
    )


def inputs_payload(ctype: str, difficulty: int) -> dict:
    raw = json.dumps({"challenge_type": ctype, "difficulty": difficulty, "memory": 128})
    return {
        "challenge": {
            "input": base64.b64encode(raw.encode()).decode(),
            "hmac": "hm",
            "region": "eu-central-1",
        }
    }


def mock_client(
    ctype: str = "SHA256",
    difficulty: int = 4,
    verify_json: dict | None = None,
    initial_headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in {SITE, f"{SITE}/"}:
            return httpx.Response(202, text=challenge_page(), headers=initial_headers or {})
        if url == SCRIPT_URL:
            return httpx.Response(200, content=SCRIPT_BYTES)
        if url.startswith(f"{CHAL_URL}/inputs"):
            return httpx.Response(200, json=inputs_payload(ctype, difficulty))
        if url.endswith(("/verify", "/mp_verify")):
            return httpx.Response(200, json=verify_json or {"token": TOKEN})
        return httpx.Response(404, text=f"unexpected: {url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_encode_format() -> None:
    encoded = encode({"a": 1})
    checksum, raw = encoded.split("#", 1)
    assert len(checksum) == 8
    assert json.loads(raw) == {"a": 1}


def test_encrypt_decrypt_roundtrip() -> None:
    plaintext = 'DEADBEEF#{"x":1}'
    assert decrypt(encrypt(plaintext)).decode() == plaintext


def test_check_zeros() -> None:
    assert _check_zeros(b"\x00\x0f\xff", 12)
    assert not _check_zeros(b"\x00\x0f\xff", 13)
    assert _check_zeros(b"\xff\x00", 0)
    assert not _check_zeros(b"\xff\x00", 1)


def test_solve_pow_sha256_finds_valid_nonce() -> None:
    nonce = _solve_pow("input", "CHECKSUM", 8, "SHA256", 128, time.monotonic() + 10)
    digest = hashlib.sha256(f"inputCHECKSUM{nonce}".encode()).digest()
    assert _check_zeros(digest, 8)


def test_solve_pow_scrypt_uses_stdlib() -> None:
    nonce = _solve_pow("input", "CHECKSUM", 4, "HashcashScrypt", 128, time.monotonic() + 10)
    digest = hashlib.scrypt(
        f"inputCHECKSUM{nonce}".encode(), salt=b"CHECKSUM", n=128, r=8, p=1, dklen=32
    )
    assert _check_zeros(digest, 4)


def test_solve_pow_budget_exceeded() -> None:
    with pytest.raises(WafPowTimeout):
        _solve_pow("input", "CHECKSUM", 64, "SHA256", 128, time.monotonic())


@pytest.mark.parametrize(
    ("status", "action", "expected"),
    [
        (200, "captcha", WafCaptchaRequired),
        (200, "block", WafBlocked),
        (429, "", WafRateLimited),
        (403, "", WafSolveFailed),
        (200, "puzzle", WafUnsupportedChallenge),
    ],
)
def test_reject_waf_response_taxonomy(status: int, action: str, expected: type) -> None:
    headers = {"x-amzn-waf-action": action} if action else {}
    response = httpx.Response(status, headers=headers)
    with pytest.raises(expected):
        AwsWafSolver._reject_waf_response(response)
    # plain challenge action and clean responses pass through
    challenge = httpx.Response(202, headers={"x-amzn-waf-action": "challenge"})
    AwsWafSolver._reject_waf_response(challenge)
    AwsWafSolver._reject_waf_response(httpx.Response(200))


def test_require_allowed_url() -> None:
    _require_allowed_url(SITE)
    _require_allowed_url(CHAL_URL)
    with pytest.raises(WafUnsupportedChallenge):
        _require_allowed_url("https://evil.example.com")


async def test_solve_full_flow_sha256() -> None:
    solver = AwsWafSolver(client=mock_client("SHA256", 4))
    assert await solver.solve(SITE, UA) == TOKEN


async def test_solve_full_flow_bandwidth() -> None:
    solver = AwsWafSolver(client=mock_client("NetworkBandwidth", 1))
    assert await solver.solve(SITE, UA) == TOKEN


async def test_solve_rejects_unknown_script_hash() -> None:
    solver = AwsWafSolver(
        client=mock_client(), script_hash_checker=lambda digest: digest == "approved"
    )
    with pytest.raises(WafScriptVersionUnknown):
        await solver.solve(SITE, UA)


async def test_solve_accepts_approved_script_hash() -> None:
    solver = AwsWafSolver(
        client=mock_client("SHA256", 4),
        script_hash_checker=lambda digest: digest == SCRIPT_SHA256,
    )
    assert await solver.solve(SITE, UA) == TOKEN


async def test_solve_captcha_action_fail_closed() -> None:
    solver = AwsWafSolver(client=mock_client(initial_headers={"x-amzn-waf-action": "captcha"}))
    with pytest.raises(WafCaptchaRequired):
        await solver.solve(SITE, UA)


async def test_solve_no_token_returned() -> None:
    solver = AwsWafSolver(client=mock_client(verify_json={"success": False}))
    with pytest.raises(WafSolveFailed):
        await solver.solve(SITE, UA)


async def test_solve_derives_challenge_base_from_single_quoted_script_src() -> None:
    script_url = "https://abc.edge.sdk.awswaf.com/proto-v2/ABC_123/challenge.js?rev=7"
    challenge_base = script_url.split("/challenge.js", 1)[0]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in {SITE, f"{SITE}/"}:
            html = f"<html><script async src='{script_url}'></script></html>"
            return httpx.Response(202, text=html, headers={"x-amzn-waf-action": "challenge"})
        if url.startswith(f"{challenge_base}/inputs"):
            return httpx.Response(200, json=inputs_payload("SHA256", 4))
        if url.endswith("/verify"):
            return httpx.Response(200, json={"token": TOKEN})
        return httpx.Response(404, text=f"unexpected: {url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    solver = AwsWafSolver(client=client)
    assert await solver.solve(SITE, UA) == TOKEN
    await client.aclose()
