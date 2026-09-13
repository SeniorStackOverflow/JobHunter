"""Pure-Python AWS WAF challenge solver (no browser, no TLS impersonation).

Protocol reimplementation vendored from
https://github.com/Switch3301/Aws-Waf-Solver (pinned source; see waf/UPSTREAM.md,
commit fed489c54fe2eb10a6dfac5b4d4c5dfcb06b8808). The transport was ported from
``rnet`` to plain ``httpx`` and ``pyscrypt`` to stdlib ``hashlib.scrypt``; the
ported scheme is exactly what was verified live against rabota.md on
2026-09-12 (3/3 consecutive solves, listing/category/detail/AJAX all 200).

The solver never logs the token and refuses anything other than
``x-amzn-waf-action: challenge``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.crawlers.adapters.rabota_md.waf.crypto import encode, encrypt
from app.crawlers.adapters.rabota_md.waf.errors import (
    WafBlocked,
    WafCaptchaRequired,
    WafPowTimeout,
    WafRateLimited,
    WafScriptVersionUnknown,
    WafSolveFailed,
    WafUnsupportedChallenge,
)
from app.crawlers.adapters.rabota_md.waf.metrics import build_metrics
from app.crawlers.adapters.rabota_md.waf.signals import build_signal
from app.crawlers.http import AsyncRateLimiter, SecureHttpClient
from app.security.ssrf import Resolver

RE_CHAL_SAME = re.compile(r"(/__challenge_[A-Za-z0-9]+/[a-f0-9]+/[a-f0-9]+)")
RE_CHAL_EXT = re.compile(
    r"(https://[a-z0-9]+\.[a-z0-9]+\.[a-z0-9-]+\.token\.awswaf\.com/[^/\s\"]+/[^/\s\"]+/[^/\s\"]+)"
)
RE_CHAL_SDK = re.compile(
    r"(https://[a-z0-9]+\.edge\.sdk\.awswaf\.com/[a-z0-9]+/[a-z0-9]+)/challenge\.js"
)
RE_CHAL_SCRIPT = re.compile(r'src="(https://[^"]*awswaf\.com[^"]*challenge\.js[^"]*)"')
RE_GOKU = re.compile(r"window\.gokuProps\s*=\s*(\{[^}]+\})")

AWS_WAF_ACTION_HEADER = "x-amzn-waf-action"

VERIFY_ENDPOINT = {
    "HashcashScrypt": "verify",
    "SHA256": "verify",
    "NetworkBandwidth": "mp_verify",
}

BANDWIDTH_SIZES = {1: 1024, 2: 10240, 3: 102400, 4: 1048576, 5: 10485760}

BRANDS = {
    0: '"Not/A)Brand";v="8", "Chromium";v="{v}", "Google Chrome";v="{v}"',
    1: '"Not A(Brand";v="24", "Chromium";v="{v}", "Google Chrome";v="{v}"',
    2: '"Chromium";v="{v}", "Not(A:Brand";v="24", "Google Chrome";v="{v}"',
    3: '"Not:A-Brand";v="8", "Chromium";v="{v}", "Google Chrome";v="{v}"',
}

# The solver only ever talks to the target site and the AWS WAF token endpoints.
_ALLOWED_HOSTS = ("rabota.md", "www.rabota.md")
_ALLOWED_HOST_SUFFIXES = (".token.awswaf.com", ".sdk.awswaf.com")


def _parse_ua(ua: str) -> tuple[str, str]:
    match = re.search(r"Chrome/(\d+)", ua)
    version = match.group(1) if match else "136"
    platform = "Windows" if "windows" in ua.lower() else "Linux"
    brand = BRANDS[int(version) % 4].replace("{v}", version)
    return brand, platform


def _nav_headers(ua: str) -> dict[str, str]:
    brand, platform = _parse_ua(ua)
    return {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
            "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": brand,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{platform}"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": ua,
    }


def _api_headers(site: str, ua: str, same_origin: bool) -> dict[str, str]:
    brand, platform = _parse_ua(ua)
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "ect": "4g",
        "origin": site,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{site}/",
        "sec-ch-ua": brand,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{platform}"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin" if same_origin else "cross-site",
        "user-agent": ua,
    }


def _check_zeros(digest: bytes, difficulty: int) -> bool:
    zeros = 0
    for byte in digest:
        if byte == 0:
            zeros += 8
        else:
            for bit in range(7, -1, -1):
                if (byte & (1 << bit)) == 0:
                    zeros += 1
                else:
                    break
            break
    return zeros >= difficulty


def _solve_bandwidth(difficulty: int) -> str:
    size = BANDWIDTH_SIZES.get(difficulty)
    if not size:
        return base64.b64encode(b"\x00" * 1024).decode()
    return base64.b64encode(b"\x00" * size).decode()


def _solve_pow(
    challenge_input: str,
    checksum: str,
    difficulty: int,
    ctype: str,
    memory: int,
    deadline: float,
) -> str:
    for nonce in range(100_000_000):
        if nonce % 256 == 0 and time.monotonic() > deadline:
            raise WafPowTimeout(f"proof-of-work exceeded its budget (type={ctype})")
        if ctype == "HashcashScrypt":
            digest = hashlib.scrypt(
                f"{challenge_input}{checksum}{nonce}".encode(),
                salt=checksum.encode(),
                n=memory,
                r=8,
                p=1,
                dklen=32,
            )
        else:
            digest = hashlib.sha256(f"{challenge_input}{checksum}{nonce}".encode()).digest()
        if _check_zeros(digest, difficulty):
            return str(nonce)
    raise WafPowTimeout(f"proof-of-work nonce space exhausted (type={ctype})")


def _require_allowed_url(url: str) -> None:
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    if host in _ALLOWED_HOSTS or any(host.endswith(suffix) for suffix in _ALLOWED_HOST_SUFFIXES):
        return
    raise WafUnsupportedChallenge(f"solver URL outside allowlist: {host}")


class AwsWafSolver:
    """Solves the AWS WAF ``challenge`` action and returns an ``aws-waf-token``.

    ``script_hash_checker`` (ScriptWatchdog seam) receives the sha256 hex digest of
    ``challenge.js`` and must return True for a canary-approved version. Without a
    checker every script version is accepted (DEV/spike mode only).
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        pow_budget_seconds: float = 30.0,
        script_hash_checker: Callable[[str], bool] | None = None,
        client: SecureHttpClient | httpx.AsyncClient | None = None,
        requests_per_minute: int = 50,
        minimum_interval_seconds: float = 1.2,
        max_redirects: int = 3,
        resolver: Resolver | None = None,
        rate_limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._pow_budget = pow_budget_seconds
        self._script_hash_checker = script_hash_checker
        self._client = client
        self._requests_per_minute = requests_per_minute
        self._minimum_interval_seconds = minimum_interval_seconds
        self._max_redirects = max_redirects
        self._resolver = resolver
        self._rate_limiter = rate_limiter
        self.last_script_hash: str | None = None

    async def solve(self, site: str, user_agent: str) -> str:
        site = site.rstrip("/")
        _require_allowed_url(site)
        client = self._client or SecureHttpClient(
            allowed_domains=(
                "rabota.md",
                "www.rabota.md",
                "token.awswaf.com",
                "sdk.awswaf.com",
            ),
            user_agent=user_agent,
            requests_per_minute=self._requests_per_minute,
            minimum_interval_seconds=self._minimum_interval_seconds,
            timeout_seconds=self._timeout,
            max_redirects=self._max_redirects,
            resolver=self._resolver,
            rate_limiter=self._rate_limiter,
        )
        try:
            return await self._solve_with_client(client, site, user_agent)
        finally:
            if self._client is None:
                await client.aclose()

    async def _solve_with_client(
        self, client: SecureHttpClient | httpx.AsyncClient, site: str, user_agent: str
    ) -> str:
        domain = site.split("//")[1].split("/")[0]
        challenge_url, same_origin, goku_props = await self._discover(client, site, user_agent)
        headers = _api_headers(site, user_agent, same_origin)
        token: str | None = None

        for round_index in range(2):
            has_token = round_index > 0
            metrics, fp_metrics = build_metrics(has_token=has_token)
            fingerprint = build_signal(f"{site}/", fp_metrics, user_agent)
            encoded = encode(fingerprint)
            checksum = encoded.split("#")[0]
            encrypted = encrypt(encoded)

            inputs_started = time.time()
            inputs_response = await self._get(
                client, f"{challenge_url}/inputs?client=browser", headers=headers
            )
            self._reject_waf_response(inputs_response)
            inputs_latency = round((time.time() - inputs_started) * 1000, 1)
            inputs = inputs_response.json()
            challenge = inputs["challenge"]
            decoded = json.loads(base64.b64decode(challenge["input"]))
            ctype = str(decoded.get("challenge_type", ""))
            difficulty = int(decoded.get("difficulty", 1))
            memory = int(decoded.get("memory", 128))
            if ctype not in VERIFY_ENDPOINT:
                raise WafUnsupportedChallenge(f"unknown challenge type: {ctype!r}")

            if has_token:
                metrics.insert(0, {"name": "0", "value": inputs_latency, "unit": "2"})

            endpoint = VERIFY_ENDPOINT[ctype]
            if ctype == "NetworkBandwidth":
                solution_data = _solve_bandwidth(difficulty)
                body, content_type = self._build_multipart(
                    domain, challenge, solution_data, checksum, encrypted, metrics, goku_props
                )
            else:
                deadline = time.monotonic() + self._pow_budget
                solution = await asyncio.to_thread(
                    _solve_pow, challenge["input"], checksum, difficulty, ctype, memory, deadline
                )
                body = self._build_body(
                    domain, challenge, solution, checksum, encrypted, metrics, goku_props
                )
                content_type = "text/plain;charset=UTF-8"

            verify_response = await self._post(
                client,
                f"{challenge_url}/{endpoint}",
                content=body,
                headers={**headers, "content-type": content_type},
            )
            self._reject_waf_response(verify_response)
            result = verify_response.json()
            token = result.get("token", token)
            if token is None:
                raise WafSolveFailed(
                    f"token endpoint returned no token (HTTP {verify_response.status_code})"
                )

        if token is None:  # pragma: no cover - loop above guarantees a token or raises
            raise WafSolveFailed("solve finished without a token")
        return token

    async def _discover(
        self, client: SecureHttpClient | httpx.AsyncClient, site: str, user_agent: str
    ) -> tuple[str, bool, dict[str, Any] | None]:
        response = await self._get(client, site, headers=_nav_headers(user_agent))
        self._reject_waf_response(response)
        html = response.text

        match = RE_CHAL_SAME.search(html)
        if match:
            challenge_url, same_origin = f"{site}{match.group(1)}", True
        else:
            match = RE_CHAL_EXT.search(html) or RE_CHAL_SDK.search(html)
            if not match:
                raise WafUnsupportedChallenge("challenge URL not found on the 202 page")
            challenge_url, same_origin = match.group(1), False
        _require_allowed_url(challenge_url)

        if self._script_hash_checker is not None:
            script_match = RE_CHAL_SCRIPT.search(html)
            if script_match is None:
                raise WafUnsupportedChallenge("challenge.js URL not found on the 202 page")
            script_url = script_match.group(1)
            _require_allowed_url(script_url)
            script_response = await self._get(client, script_url, headers=_nav_headers(user_agent))
            self._reject_waf_response(script_response)
            digest = hashlib.sha256(script_response.content).hexdigest()
            self.last_script_hash = digest
            if not self._script_hash_checker(digest):
                raise WafScriptVersionUnknown(
                    f"no fresh WAF compatibility canary for challenge.js sha256={digest}"
                )

        goku_props: dict[str, Any] | None = None
        goku_match = RE_GOKU.search(html)
        if goku_match:
            goku_props = json.loads(goku_match.group(1))
        return challenge_url, same_origin, goku_props

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        raw = response.headers.get("retry-after", "")
        try:
            return max(0.0, min(float(raw), 30.0)) if raw else 5.0
        except ValueError:
            return 5.0

    async def _get(
        self,
        client: SecureHttpClient | httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
    ) -> httpx.Response:
        response = await client.get(url, headers=headers)
        if response.status_code == 429:
            await asyncio.sleep(self._retry_after_seconds(response))
            response = await client.get(url, headers=headers)
        return response

    async def _post(
        self,
        client: SecureHttpClient | httpx.AsyncClient,
        url: str,
        *,
        content: str,
        headers: dict[str, str],
    ) -> httpx.Response:
        if isinstance(client, SecureHttpClient):
            response = await client.post_bounded(url, content=content, headers=headers)
        else:
            response = await client.post(url, content=content, headers=headers)
        if response.status_code == 429:
            await asyncio.sleep(self._retry_after_seconds(response))
            if isinstance(client, SecureHttpClient):
                response = await client.post_bounded(url, content=content, headers=headers)
            else:
                response = await client.post(url, content=content, headers=headers)
        return response

    @staticmethod
    def _reject_waf_response(response: httpx.Response) -> None:
        if response.status_code == 429:
            raise WafRateLimited("AWS WAF rate limited the solver")
        action = response.headers.get(AWS_WAF_ACTION_HEADER, "").casefold()
        if action == "captcha":
            raise WafCaptchaRequired("AWS WAF requested a CAPTCHA; fail-closed by policy")
        if action == "block":
            raise WafBlocked("AWS WAF hard-blocked the request")
        if action and action != "challenge":
            raise WafUnsupportedChallenge(f"unknown x-amzn-waf-action: {action!r}")

    @staticmethod
    def _build_body(
        domain: str,
        challenge: dict[str, Any],
        solution: str,
        checksum: str,
        encrypted: str,
        metrics: list[dict[str, object]],
        goku_props: dict[str, Any] | None,
    ) -> str:
        payload: dict[str, Any] = {
            "challenge": challenge,
            "solution": solution,
            "signals": [{"name": "Zoey", "value": {"Present": encrypted}}],
            "checksum": checksum,
            "existing_token": None,
            "client": "Browser",
            "domain": domain,
            "metrics": metrics,
        }
        if goku_props:
            payload["goku_props"] = goku_props
        return json.dumps(payload, separators=(",", ":"))

    @staticmethod
    def _build_multipart(
        domain: str,
        challenge: dict[str, Any],
        solution_data: str,
        checksum: str,
        encrypted: str,
        metrics: list[dict[str, object]],
        goku_props: dict[str, Any] | None,
    ) -> tuple[str, str]:
        import random
        import string

        meta: dict[str, Any] = {
            "challenge": challenge,
            "solution": None,
            "signals": [{"name": "Zoey", "value": {"Present": encrypted}}],
            "checksum": checksum,
            "existing_token": None,
            "client": "Browser",
            "domain": domain,
            "metrics": metrics,
        }
        if goku_props:
            meta["goku_props"] = goku_props

        boundary = "----WebKitFormBoundary" + "".join(
            random.choices(string.ascii_letters + string.digits, k=16)  # noqa: S311 - form boundary
        )
        meta_json = json.dumps(meta, separators=(",", ":"))
        parts = [
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="solution_data"\r\n\r\n{solution_data}',
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="solution_metadata"\r\n\r\n{meta_json}',
            f"--{boundary}--\r\n",
        ]
        return "\r\n".join(parts), f"multipart/form-data; boundary={boundary}"
