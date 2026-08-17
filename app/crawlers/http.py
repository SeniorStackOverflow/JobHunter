from __future__ import annotations

import asyncio
import ipaddress
import time
from collections.abc import Iterable
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.security.ssrf import (
    Resolver,
    ValidatedURL,
    preferred_public_address,
    validate_outbound_url,
    validate_redirect,
)

DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class HttpFetcher(Protocol):
    async def get(self, url: str) -> httpx.Response: ...


class CrawlerResponseTooLarge(httpx.HTTPError):
    """The decoded source response exceeded the bounded crawler budget."""


class AsyncRateLimiter:
    def __init__(self, requests_per_minute: int, minimum_interval_seconds: float = 0.0) -> None:
        self._interval = max(60.0 / requests_per_minute, minimum_interval_seconds)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed = time.monotonic() + self._interval


class SecureHttpClient:
    def __init__(
        self,
        allowed_domains: Iterable[str],
        user_agent: str,
        requests_per_minute: int = 20,
        minimum_interval_seconds: float = 0.0,
        timeout_seconds: float = 20.0,
        max_redirects: int = 5,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        pin_resolved_addresses: bool | None = None,
    ) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self.allowed_domains = tuple(allowed_domains)
        self._resolver = resolver
        self._max_redirects = max_redirects
        self._max_response_bytes = max_response_bytes
        self._pin_resolved_addresses = (
            transport is None if pin_resolved_addresses is None else pin_resolved_addresses
        )
        self._limiter = AsyncRateLimiter(requests_per_minute, minimum_interval_seconds)
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            # Crawler destinations are controlled by the source allowlist. Inheriting
            # HTTP(S)_PROXY would silently move DNS and connection policy to an
            # unrelated process and undermine the SSRF boundary.
            trust_env=False,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/json;q=0.9"},
            # A pinned IP is an HTTP origin from the pool's perspective. Disabling
            # keep-alive prevents a TLS connection opened with one hostname/SNI from
            # being reused for another allowlisted hostname that shares the same IP.
            limits=httpx.Limits(max_keepalive_connections=0),
            transport=transport,
        )

    @staticmethod
    def _pinned_target(validated: ValidatedURL) -> tuple[str, str]:
        parsed = urlsplit(validated.url)
        address = preferred_public_address(validated.addresses)
        authority = f"[{address}]" if ipaddress.ip_address(address).version == 6 else address
        port = parsed.port
        if port is not None:
            authority = f"{authority}:{port}"
        target = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
        host_header = validated.hostname
        if port is not None and port not in {80, 443}:
            host_header = f"{host_header}:{port}"
        return target, host_header

    async def _bounded_get(self, validated: ValidatedURL) -> httpx.Response:
        request_url = validated.url
        headers: dict[str, str] | None = None
        extensions: dict[str, str] | None = None
        if self._pin_resolved_addresses:
            request_url, host_header = self._pinned_target(validated)
            headers = {"Host": host_header}
            if urlsplit(validated.url).scheme == "https":
                extensions = {"sni_hostname": validated.hostname}

        request = self._client.build_request(
            "GET",
            request_url,
            headers=headers,
            extensions=extensions,
        )
        response = await self._client.send(request, stream=True)
        try:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_response_bytes:
                    raise CrawlerResponseTooLarge(
                        "source response exceeded the configured byte limit"
                    )
                chunks.append(chunk)
            # aiter_bytes returns decoded content. Remove stale representation headers
            # so the reconstructed response cannot attempt to decode it a second time.
            safe_headers = [
                (key, value)
                for key, value in response.headers.multi_items()
                if key.casefold() not in {"content-encoding", "content-length"}
            ]
            return httpx.Response(
                response.status_code,
                headers=safe_headers,
                content=b"".join(chunks),
                request=httpx.Request("GET", validated.url),
                extensions={
                    key: value
                    for key, value in response.extensions.items()
                    if key != "network_stream"
                },
            )
        finally:
            await response.aclose()

    async def get(self, url: str) -> httpx.Response:
        validated = await validate_outbound_url(url, self.allowed_domains, self._resolver)
        current = validated.url
        for _ in range(self._max_redirects + 1):
            await self._limiter.wait()
            response = await self._bounded_get(validated)
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            validated = await validate_redirect(
                current,
                location,
                self.allowed_domains,
                self._resolver,
            )
            current = validated.url
        raise httpx.TooManyRedirects("source exceeded redirect limit", request=response.request)

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "AsyncRateLimiter",
    "CrawlerResponseTooLarge",
    "HttpFetcher",
    "SecureHttpClient",
]
