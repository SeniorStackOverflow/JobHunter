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
    WafSolverCompatibilityError,
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
HASHED_SCRYPT = "h72f957df656e80ba55f5d8ce2e8c7ccb59687dba3bfb273d54b08a261b2f3002"
HASHED_SHA256 = "h7b0c470f0cfe3a80a9e26526ad185f484f6817d0832712a4a37a908786a6a67f"
HASHED_BANDWIDTH = "ha9faaffd31b4d5ede2a2e19d2d7fd525f66fee61911511960dcbb52d3c48ce25"


def challenge_page() -> str:
    return (
        "<html><head><script>window.awsWafCookieDomainList = [];\n"
        'window.gokuProps = {"key":"k","iv":"i","context":"c"};</script>\n'
        f'<script src="{SCRIPT_URL}"></script></head><body>'
        '<div id="challenge-container"></div></body></html>'
    )


def modern_inputs_payload(ctype: str, difficulty: int, *, input_bytes: bytes = b"opaque") -> dict:
    return {
        "challenge": {
            "input": base64.b64encode(input_bytes).decode(),
            "region": "eu-central-1",
        },
        "challenge_type": ctype,
        "difficulty": difficulty,
    }


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
        f"inputCHECKSUM{nonce}".encode(), salt=b"CHECKSUM", n=128, r=8, p=1, dklen=16
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
        if url == script_url:
            return httpx.Response(200, content=SCRIPT_BYTES)
        if url.startswith(f"{challenge_base}/inputs"):
            return httpx.Response(200, json=inputs_payload("SHA256", 4))
        if url.endswith("/verify"):
            return httpx.Response(200, json={"token": TOKEN})
        return httpx.Response(404, text=f"unexpected: {url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    solver = AwsWafSolver(client=client)
    assert await solver.solve(SITE, UA) == TOKEN
    await client.aclose()


async def test_solve_classifies_invalid_challenge_input_encoding() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in {SITE, f"{SITE}/"}:
            return httpx.Response(202, text=challenge_page())
        if url == SCRIPT_URL:
            return httpx.Response(200, content=SCRIPT_BYTES)
        if url.startswith(f"{CHAL_URL}/inputs"):
            payload = inputs_payload("SHA256", 4)
            payload["challenge"]["input"] = base64.b64encode(b"\xff\xfe\xfd").decode()
            return httpx.Response(200, json=payload)
        return httpx.Response(404, text=f"unexpected: {url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    solver = AwsWafSolver(client=client)

    with pytest.raises(WafSolverCompatibilityError) as caught:
        await solver.solve(SITE, UA)

    assert caught.value.stage == "inputs"
    assert caught.value.error_type == "LegacyMetadataUnavailable"
    await client.aclose()


async def test_solve_modern_hashed_bandwidth_with_opaque_input() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in {SITE, f"{SITE}/"}:
            return httpx.Response(202, text=challenge_page())
        if url == SCRIPT_URL:
            return httpx.Response(200, content=SCRIPT_BYTES)
        if url.startswith(f"{CHAL_URL}/inputs"):
            return httpx.Response(
                200,
                json=modern_inputs_payload(
                    HASHED_BANDWIDTH,
                    3,
                    input_bytes=bytes.fromhex("73cd5fd5dddaef5d5ce36d786f9f39d3"),
                ),
            )
        if url.endswith("/mp_verify"):
            seen_paths.append("/mp_verify")
            body = request.content.decode()
            assert 'name="solution_data"' in body
            return httpx.Response(200, json={"token": TOKEN})
        return httpx.Response(404, text=f"unexpected: {url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    solver = AwsWafSolver(client=client)
    assert await solver.solve(SITE, UA) == TOKEN
    assert seen_paths == ["/mp_verify", "/mp_verify"]
    await client.aclose()


@pytest.mark.parametrize(
    ("hashed_type", "expected_path"),
    [
        (HASHED_SHA256, "/verify"),
        (HASHED_SCRYPT, "/verify"),
    ],
)
async def test_solve_modern_hashed_pow_types(
    hashed_type: str,
    expected_path: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in {SITE, f"{SITE}/"}:
            return httpx.Response(202, text=challenge_page())
        if url == SCRIPT_URL:
            return httpx.Response(200, content=SCRIPT_BYTES)
        if url.startswith(f"{CHAL_URL}/inputs"):
            return httpx.Response(
                200,
                json=modern_inputs_payload(hashed_type, 2, input_bytes=b"proof-seed"),
            )
        if url.endswith(expected_path):
            return httpx.Response(200, json={"token": TOKEN})
        return httpx.Response(404, text=f"unexpected: {url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    solver = AwsWafSolver(client=client, pow_budget_seconds=5)
    assert await solver.solve(SITE, UA) == TOKEN
    await client.aclose()


async def test_probe_challenge_validates_modern_inputs_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in {SITE, f"{SITE}/"}:
            return httpx.Response(202, text=challenge_page())
        if url == SCRIPT_URL:
            return httpx.Response(200, content=SCRIPT_BYTES)
        if url.startswith(f"{CHAL_URL}/inputs"):
            return httpx.Response(200, json=modern_inputs_payload(HASHED_BANDWIDTH, 3))
        return httpx.Response(404, text=f"unexpected: {url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    solver = AwsWafSolver(client=client)
    assert await solver.probe_challenge(SITE, UA) == SCRIPT_SHA256
    await client.aclose()


async def test_unknown_modern_type_is_solver_compatibility_failure() -> None:
    unknown = "h" + "1" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in {SITE, f"{SITE}/"}:
            return httpx.Response(202, text=challenge_page())
        if url == SCRIPT_URL:
            return httpx.Response(200, content=SCRIPT_BYTES)
        if url.startswith(f"{CHAL_URL}/inputs"):
            return httpx.Response(200, json=modern_inputs_payload(unknown, 3))
        return httpx.Response(404, text=f"unexpected: {url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    solver = AwsWafSolver(client=client)

    with pytest.raises(WafSolverCompatibilityError) as caught:
        await solver.probe_challenge(SITE, UA)

    assert caught.value.error_type == "UnknownChallengeType"
    await client.aclose()


async def test_unknown_hash_can_use_dynamic_mp_verify_mapping() -> None:
    unknown = "hdeadbeef" + "1" * 56
    script = (
        b"var m={};m['hdeadbeef'+'rest']='mp_verify';"
        b"case 0x1:return 0x400;"
        b"case 0x2:return x(0xa,0x400);"
        b"case 0x3:return x(0x64,0x400);"
        b"case 0x4:return x(0x1,0x100000);"
        b"case 0x5:return x(0xa,0x100000)"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in {SITE, f"{SITE}/"}:
            return httpx.Response(202, text=challenge_page())
        if url == SCRIPT_URL:
            return httpx.Response(200, content=script)
        if url.startswith(f"{CHAL_URL}/inputs"):
            return httpx.Response(200, json=modern_inputs_payload(unknown, 1))
        if url.endswith("/mp_verify"):
            return httpx.Response(200, json={"token": TOKEN})
        return httpx.Response(404, text=f"unexpected: {url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    solver = AwsWafSolver(client=client)
    assert await solver.solve(SITE, UA) == TOKEN
    await client.aclose()
