"""waf_http transport: plain-HTTP Rabota.md fetcher authenticated by aws-waf-token.

Implements the WafHttpClient design from docs/sources/rabota-md-http.md.
Challenge detection uses only the 202 status and the ``x-amzn-waf-action``
header — the 202 body is empty unless browser Accept headers are sent, so it
must never be inspected (spike finding 2026-09-12, doc п. 7).
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.crawlers.adapters.rabota_md.waf.errors import (
    WafBlocked,
    WafCaptchaRequired,
    WafChallengeRequired,
    WafPostContractError,
    WafRateLimited,
)
from app.crawlers.adapters.rabota_md.waf.token_provider import WafTokenProvider
from app.crawlers.browser import AWS_WAF_ACTION_HEADER
from app.crawlers.http import SecureHttpClient
from app.observability.metrics import RABOTA_WAF_CHALLENGE

_TOKEN_COOKIE = "aws-waf-token"  # noqa: S105 - cookie name, not a secret


def _ajax_headers(referer: str) -> dict[str, str]:
    """Canonical browser header set required by the pagination POST (recon п. 6)."""
    parsed = urlsplit(referer)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return {
        "x-requested-with": "XMLHttpRequest",
        "origin": origin,
        "referer": referer,
        "accept": "application/json, text/javascript, */*; q=0.01",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def _category_referer(url: str) -> str:
    """/ru/vacancies/category/<slug>/3 -> /ru/vacancies/category/<slug>."""
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[-1].isdigit():
        parts = parts[:-1]
    return urlunsplit(("https", parsed.netloc, "/" + "/".join(parts), "", ""))


class WafHttpClient:
    def __init__(
        self,
        client: SecureHttpClient,
        token_provider: WafTokenProvider,
    ) -> None:
        self._client = client
        self._tokens = token_provider

    async def get(self, url: str) -> httpx.Response:
        return await self._request_with_token("GET", url)

    async def post_html_fragment(self, url: str) -> httpx.Response:
        response = await self._request_with_token(
            "POST", url, extra_headers=_ajax_headers(_category_referer(url))
        )
        if response.status_code == 403:
            # Canonical headers were used by construction: the POST contract broke.
            raise WafPostContractError(f"Rabota.md AJAX pagination rejected POST {url}")
        if response.status_code != 200:
            return response
        try:
            payload = json.loads(response.text)
            content = payload["data"]["content"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise WafPostContractError(
                "Rabota.md AJAX fragment response did not contain data.content"
            ) from exc
        if payload.get("success") is not True or not isinstance(content, str):
            raise WafPostContractError("Rabota.md AJAX fragment response was not successful")
        final = str(response.url)
        return httpx.Response(
            200,
            text=content,
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("POST", final),
            extensions={"job_agent_final_url": final, "job_agent_fragment": True},
        )

    async def _request_with_token(
        self, method: str, url: str, extra_headers: dict[str, str] | None = None
    ) -> httpx.Response:
        response = await self._send(method, url, extra_headers)
        response = await self._retry_rate_limit(method, url, extra_headers, response)
        if not self._is_challenge(response):
            self._reject_terminal_waf(response)
            return response
        # Stale/missing token: invalidate, single-flight refresh, one retry.
        await self._tokens.invalidate()
        await self._tokens.refresh_token()
        response = await self._send(method, url, extra_headers)
        response = await self._retry_rate_limit(method, url, extra_headers, response)
        if self._is_challenge(response):
            raise WafChallengeRequired(f"AWS WAF challenge persists after token refresh: {url}")
        self._reject_terminal_waf(response)
        return response

    async def _send(
        self, method: str, url: str, extra_headers: dict[str, str] | None
    ) -> httpx.Response:
        token = await self._tokens.get_token() or await self._tokens.refresh_token()
        headers = {**(extra_headers or {}), "cookie": f"{_TOKEN_COOKIE}={token}"}
        if method == "POST":
            return await self._client.post_bounded(url, content="", headers=headers)
        return await self._client.get(url, headers=headers)

    async def _retry_rate_limit(
        self,
        method: str,
        url: str,
        extra_headers: dict[str, str] | None,
        response: httpx.Response,
    ) -> httpx.Response:
        if response.status_code != 429:
            return response
        raw = response.headers.get("retry-after", "")
        try:
            delay = max(0.0, min(float(raw), 30.0)) if raw else 5.0
        except ValueError:
            delay = 5.0
        await asyncio.sleep(delay)
        return await self._send(method, url, extra_headers)

    @staticmethod
    def _is_challenge(response: httpx.Response) -> bool:
        if (
            response.status_code == 202
            and response.headers.get(AWS_WAF_ACTION_HEADER, "").casefold() == "challenge"
        ):
            RABOTA_WAF_CHALLENGE.inc()
            return True
        return False

    @staticmethod
    def _reject_terminal_waf(response: httpx.Response) -> None:
        if response.status_code == 429:
            raise WafRateLimited("Rabota.md rate limited the request")
        if response.status_code != 202:
            return
        action = response.headers.get(AWS_WAF_ACTION_HEADER, "").casefold()
        if action == "captcha":
            raise WafCaptchaRequired("Rabota.md requested a CAPTCHA; fail-closed by policy")
        if action == "block":
            raise WafBlocked("Rabota.md hard-blocked the request")

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._tokens.aclose()
