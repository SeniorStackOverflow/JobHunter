from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
import redis.asyncio as aioredis
import structlog

from app.crawlers.adapters.rabota_md.errors import (
    RabotaMdDegradedError,
    RabotaMdEgressError,
    RabotaMdWafFailClosedError,
)
from app.crawlers.adapters.rabota_md.fetcher import RabotaMdFetcher
from app.crawlers.adapters.rabota_md.waf.http_client import _ajax_headers
from app.observability.metrics import (
    RABOTA_PROXY_EGRESS,
    RABOTA_PROXY_FAILOVER,
    RABOTA_PROXY_POOL_ALIVE,
)

log = structlog.get_logger()

_STATE_KEY = "crawler:rabota_md:proxy_pool:state"
_DISCOVERY_LOCK_KEY = "crawler:rabota_md:proxy_pool:discovery_lock"
_DISCOVERY_LOCK_TTL_SECONDS = 120
_FREE_PROXY_SOURCES: tuple[tuple[str, Literal["lines", "geonode"]], ...] = (
    (
        "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
        "&protocol=http&proxy_format=ipport&format=text&timeout=5000",
        "lines",
    ),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "lines"),
    (
        "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1"
        "&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps",
        "geonode",
    ),
)


@dataclass(frozen=True, slots=True)
class ProxyEndpoint:
    """One stable Rabota.md egress identity.

    Tokens and browser state must never be shared across endpoint identities because
    AWS WAF binds its token to the network identity that minted it.
    """

    name: str
    url: str
    kind: Literal["primary", "free"]
    capability: Literal["direct_pagination", "waf_candidate", "full_waf"] | None = None

    @property
    def identity(self) -> str:
        parsed = urlsplit(self.url)
        host_port = f"{parsed.hostname}:{parsed.port}"
        if self.kind == "primary":
            # Changing the configured primary endpoint must never reuse WAF state from
            # the previous endpoint. Keep the raw proxy URL out of Redis/log identities.
            digest = hashlib.sha256(self.url.encode()).hexdigest()[:12]
            return f"primary:{self.name}:{digest}"
        return f"free:{host_port}"

    @property
    def token_namespace(self) -> str:
        digest = hashlib.sha256(self.identity.encode()).hexdigest()[:16]
        return f"egress:{digest}"


class RabotaProxyPool:
    """Primary A14 egress with bounded public-proxy fallback.

    Free proxies are discovered lazily only after the primary path becomes unusable.
    State lives in Redis so a worker restart keeps cooldowns and known-good fallbacks.
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        primary_url: str | None,
        target_url: str,
        user_agent: str,
        free_fallback_enabled: bool = True,
        discovery_batch: int = 240,
        validation_concurrency: int = 24,
        validation_timeout_seconds: float = 8.0,
        ban_cooldown_seconds: int = 6 * 3600,
        dead_cooldown_seconds: int = 3600,
        primary_dead_cooldown_seconds: int = 300,
    ) -> None:
        self._redis = redis
        self._primary = (
            ProxyEndpoint(name="a14", url=primary_url, kind="primary") if primary_url else None
        )
        self._target_url = target_url
        self._user_agent = user_agent
        self._free_enabled = free_fallback_enabled
        self._discovery_batch = discovery_batch
        self._validation_concurrency = validation_concurrency
        self._validation_timeout = validation_timeout_seconds
        self._ban_cooldown = ban_cooldown_seconds
        self._dead_cooldown = dead_cooldown_seconds
        self._primary_dead_cooldown = primary_dead_cooldown_seconds

    @staticmethod
    def _as_float(value: object, default: float = 0.0) -> float:
        if isinstance(value, (int, float, str)):
            try:
                return float(value)
            except ValueError:
                return default
        return default

    @staticmethod
    def _as_int(value: object, default: int = 0) -> int:
        if isinstance(value, (int, float, str)):
            try:
                return int(value)
            except ValueError:
                return default
        return default

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def next_endpoint(self, excluded: set[str]) -> ProxyEndpoint | None:
        now = time.time()
        if self._primary is not None and self._primary.identity not in excluded:
            state = await self._entry(self._primary.identity)
            if self._as_float(state.get("cooldown_until", 0)) <= now:
                await self._touch(self._primary)
                return self._primary

        candidate = await self._known_free_endpoint(excluded)
        if candidate is not None:
            await self._touch(candidate)
            return candidate
        if not self._free_enabled:
            return None

        await self.discover()
        candidate = await self._known_free_endpoint(excluded)
        if candidate is not None:
            await self._touch(candidate)
        return candidate

    async def report_success(
        self,
        endpoint: ProxyEndpoint,
        status_code: int,
        *,
        validated_capability: str | None = None,
    ) -> None:
        payload = await self._entry(endpoint.identity)
        payload.update(
            status="alive",
            kind=endpoint.kind,
            last_check=time.time(),
            last_http_status=status_code,
            cooldown_until=0,
            fails=0,
        )
        if endpoint.kind == "free":
            payload["url"] = endpoint.url
            if validated_capability is not None:
                payload["validated_capability"] = validated_capability
        await self._write_entry(endpoint.identity, payload)
        RABOTA_PROXY_EGRESS.labels(kind=endpoint.kind, outcome="success").inc()

    async def report_access_rejected(self, endpoint: ProxyEndpoint) -> None:
        payload = await self._entry(endpoint.identity)
        payload.update(
            status="banned",
            kind=endpoint.kind,
            last_check=time.time(),
            last_http_status=403,
            cooldown_until=time.time() + self._ban_cooldown,
        )
        if endpoint.kind == "free":
            payload["url"] = endpoint.url
        await self._write_entry(endpoint.identity, payload)
        RABOTA_PROXY_EGRESS.labels(kind=endpoint.kind, outcome="access_rejected").inc()

    async def report_dead(self, endpoint: ProxyEndpoint) -> None:
        payload = await self._entry(endpoint.identity)
        fails = self._as_int(payload.get("fails", 0)) + 1
        cooldown = (
            self._primary_dead_cooldown if endpoint.kind == "primary" else self._dead_cooldown
        )
        payload.update(
            status="dead",
            kind=endpoint.kind,
            last_check=time.time(),
            cooldown_until=time.time() + cooldown,
            fails=fails,
        )
        if endpoint.kind == "free":
            payload["url"] = endpoint.url
        await self._write_entry(endpoint.identity, payload)
        RABOTA_PROXY_EGRESS.labels(kind=endpoint.kind, outcome="dead").inc()

    async def discover(self) -> int:
        if not self._free_enabled:
            return 0
        acquired = await self._redis.set(
            _DISCOVERY_LOCK_KEY,
            "1",
            nx=True,
            ex=_DISCOVERY_LOCK_TTL_SECONDS,
        )
        if not acquired:
            await asyncio.sleep(1.0)
            alive = await self._known_free_count()
            RABOTA_PROXY_POOL_ALIVE.set(alive)
            return alive

        try:
            candidates = await self._fetch_candidates()
            now = time.time()
            eligible: list[str] = []
            for proxy in candidates:
                identity = f"free:{proxy}"
                state = await self._entry(identity)
                if self._as_float(state.get("cooldown_until", 0)) > now:
                    continue
                eligible.append(proxy)

            # Do not always validate the same lexicographic prefix. The hourly salt keeps
            # discovery deterministic within one run while spreading load across the lists.
            hour = int(now // 3600)
            eligible.sort(key=lambda proxy: hashlib.sha256(f"{hour}:{proxy}".encode()).digest())
            selected = eligible[: self._discovery_batch]
            semaphore = asyncio.Semaphore(self._validation_concurrency)
            await asyncio.gather(
                *(self._validate_free_proxy(proxy, semaphore) for proxy in selected)
            )
            alive = await self._known_free_count()
            RABOTA_PROXY_POOL_ALIVE.set(alive)
            log.info(
                "rabota_proxy_discovery_finished",
                candidates=len(candidates),
                validated=len(selected),
                alive=alive,
            )
            return alive
        finally:
            await self._redis.delete(_DISCOVERY_LOCK_KEY)

    async def _fetch_candidates(self) -> list[str]:
        found: set[str] = set()
        timeout = httpx.Timeout(15.0, connect=8.0)
        async with httpx.AsyncClient(
            timeout=timeout, trust_env=False, follow_redirects=True
        ) as client:
            responses = await asyncio.gather(
                *(self._fetch_source(client, url, kind) for url, kind in _FREE_PROXY_SOURCES)
            )
        for batch in responses:
            found.update(batch)
        return sorted(found)

    async def _fetch_source(
        self,
        client: httpx.AsyncClient,
        url: str,
        kind: Literal["lines", "geonode"],
    ) -> set[str]:
        found: set[str] = set()
        try:
            response = await client.get(url)
            response.raise_for_status()
            if kind == "lines":
                for raw in response.text.splitlines():
                    proxy = self._normalize_public_proxy(raw)
                    if proxy is not None:
                        found.add(proxy)
            else:
                data = response.json()
                for item in data.get("data", []):
                    if not isinstance(item, dict):
                        continue
                    proxy = self._normalize_public_proxy(
                        f"{item.get('ip', '')}:{item.get('port', '')}"
                    )
                    if proxy is not None:
                        found.add(proxy)
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log.info(
                "rabota_proxy_source_failed",
                source=urlsplit(url).hostname or "unknown",
                error_type=type(exc).__name__,
            )
        return found

    @staticmethod
    def _normalize_public_proxy(raw: str) -> str | None:
        value = raw.strip().removeprefix("http://").removeprefix("https://")
        if value.count(":") != 1:
            return None
        host, port_raw = value.rsplit(":", 1)
        try:
            address = ipaddress.ip_address(host)
            port = int(port_raw)
        except (ValueError, TypeError):
            return None
        if not address.is_global or not 1 <= port <= 65535:
            return None
        return f"{address}:{port}"

    async def _validate_free_proxy(self, proxy: str, semaphore: asyncio.Semaphore) -> None:
        endpoint = ProxyEndpoint(name="free", url=f"http://{proxy}", kind="free")
        async with semaphore:
            try:
                timeout = httpx.Timeout(
                    self._validation_timeout, connect=self._validation_timeout
                )
                async with httpx.AsyncClient(
                    proxy=endpoint.url,
                    timeout=timeout,
                    trust_env=False,
                    follow_redirects=False,
                    headers={"User-Agent": self._user_agent},
                ) as client:
                    async with client.stream("GET", self._target_url) as response:
                        status = response.status_code
                        action = response.headers.get(
                            "x-amzn-waf-action", ""
                        ).casefold()

                    if status == 202 and action == "challenge":
                        await self.report_success(
                            endpoint,
                            status,
                            validated_capability="waf_candidate",
                        )
                        return

                    if status == 200:
                        parsed = urlsplit(self._target_url)
                        origin = urlunsplit(
                            (parsed.scheme, parsed.netloc, "", "", "")
                        )
                        locale = next(
                            (part for part in parsed.path.split("/") if part),
                            "ru",
                        )
                        referer = f"{origin}/{locale}/vacancies/category/others"
                        page_url = f"{referer}/2"
                        pagination = await client.post(
                            page_url,
                            content=b"",
                            headers=_ajax_headers(referer),
                        )
                        pagination_action = pagination.headers.get(
                            "x-amzn-waf-action", ""
                        ).casefold()
                        if pagination.status_code == 200:
                            try:
                                payload = pagination.json()
                                content = (payload.get("data") or {}).get("content")
                            except (ValueError, TypeError):
                                payload, content = {}, None
                            if payload.get("success") is True and isinstance(content, str):
                                await self.report_success(
                                    endpoint,
                                    200,
                                    validated_capability="direct_pagination",
                                )
                                return
                        if pagination.status_code in {403, 429}:
                            await self.report_access_rejected(endpoint)
                        else:
                            # A POST-only challenge cannot bootstrap the current solver
                            # because the landing GET did not expose challenge metadata.
                            # Treat this egress as unusable for full scans.
                            if (
                                pagination.status_code == 202
                                and pagination_action == "challenge"
                            ):
                                await self.report_dead(endpoint)
                            else:
                                await self.report_dead(endpoint)
                        return

                if status in {403, 429}:
                    await self.report_access_rejected(endpoint)
                else:
                    await self.report_dead(endpoint)
            except httpx.TransportError:
                await self.report_dead(endpoint)

    async def _known_free_endpoint(self, excluded: set[str]) -> ProxyEndpoint | None:
        now = time.time()
        entries = cast(dict[object, object], await cast(Any, self._redis).hgetall(_STATE_KEY))
        candidates: list[tuple[int, float, ProxyEndpoint]] = []
        for raw_identity, raw_payload in entries.items():
            identity = self._decode(raw_identity)
            if identity in excluded or not identity.startswith("free:"):
                continue
            try:
                payload = json.loads(self._decode(raw_payload))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("status") != "alive":
                continue
            if self._as_float(payload.get("cooldown_until", 0)) > now:
                continue
            url = payload.get("url")
            if not isinstance(url, str) or not url.startswith("http://"):
                continue
            capability = payload.get("validated_capability")
            if capability not in {"direct_pagination", "waf_candidate", "full_waf"}:
                continue
            # Direct pagination and previously proven WAF sessions are preferred over
            # raw challenge candidates that still need a production-stack preflight.
            priority = {
                "direct_pagination": 0,
                "full_waf": 1,
                "waf_candidate": 2,
            }[capability]
            candidates.append(
                (
                    priority,
                    self._as_float(payload.get("last_used", 0)),
                    ProxyEndpoint(
                        name="free",
                        url=url,
                        kind="free",
                        capability=capability,
                    ),
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    async def _known_free_count(self) -> int:
        now = time.time()
        entries = cast(dict[object, object], await cast(Any, self._redis).hgetall(_STATE_KEY))
        count = 0
        for raw_identity, raw_payload in entries.items():
            identity = self._decode(raw_identity)
            if not identity.startswith("free:"):
                continue
            try:
                payload = json.loads(self._decode(raw_payload))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                payload.get("status") == "alive"
                and self._as_float(payload.get("cooldown_until", 0)) <= now
            ):
                count += 1
        return count

    async def _touch(self, endpoint: ProxyEndpoint) -> None:
        payload = await self._entry(endpoint.identity)
        payload.update(kind=endpoint.kind, last_used=time.time())
        if endpoint.kind == "free":
            payload["url"] = endpoint.url
        await self._write_entry(endpoint.identity, payload)

    async def _entry(self, identity: str) -> dict[str, object]:
        raw = await cast(Any, self._redis).hget(_STATE_KEY, identity)
        if raw is None:
            return {}
        try:
            value = json.loads(self._decode(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    async def _write_entry(self, identity: str, payload: dict[str, object]) -> None:
        await cast(Any, self._redis).hset(
            _STATE_KEY, identity, json.dumps(payload, separators=(",", ":"))
        )

    @staticmethod
    def _decode(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode()
        return str(value)


FetcherFactory = Callable[[ProxyEndpoint], RabotaMdFetcher]
PreflightProbe = Callable[[ProxyEndpoint, RabotaMdFetcher], Awaitable[None]]


class ProxyPoolFetcher:
    """Keep one egress sticky until it actually fails, then fail over as a whole session."""

    def __init__(
        self,
        pool: RabotaProxyPool,
        factory: FetcherFactory,
        *,
        preflight: PreflightProbe | None = None,
        max_egress_failovers: int = 4,
    ) -> None:
        self._pool = pool
        self._factory = factory
        self._preflight = preflight
        self._max_failovers = max_egress_failovers
        self._active_endpoint: ProxyEndpoint | None = None
        self._active_fetcher: RabotaMdFetcher | None = None
        self._excluded: set[str] = set()
        self._failovers = 0

    async def get(self, url: str) -> httpx.Response:
        return await self._request("get", url)

    async def post_html_fragment(self, url: str, *, referer: str | None = None) -> httpx.Response:
        return await self._request("post", url, referer=referer)

    async def _request(
        self,
        method: Literal["get", "post"],
        url: str,
        *,
        referer: str | None = None,
    ) -> httpx.Response:
        while True:
            endpoint, fetcher = await self._ensure_active()
            try:
                if method == "get":
                    response = await fetcher.get(url)
                else:
                    response = await fetcher.post_html_fragment(url, referer=referer)
            except RabotaMdWafFailClosedError:
                raise
            except RabotaMdEgressError as exc:
                if exc.access_rejected:
                    await self._pool.report_access_rejected(endpoint)
                else:
                    await self._pool.report_dead(endpoint)
                await self._rotate(endpoint, type(exc).__name__)
                continue
            except httpx.TransportError as exc:
                await self._pool.report_dead(endpoint)
                await self._rotate(endpoint, type(exc).__name__)
                continue

            await self._pool.report_success(endpoint, response.status_code)
            return response

    async def _ensure_active(self) -> tuple[ProxyEndpoint, RabotaMdFetcher]:
        if self._active_endpoint is not None and self._active_fetcher is not None:
            return self._active_endpoint, self._active_fetcher

        while True:
            endpoint = await self._pool.next_endpoint(self._excluded)
            if endpoint is None:
                raise RabotaMdDegradedError("Rabota.md proxy pool has no usable egress")
            self._active_endpoint = endpoint
            self._active_fetcher = self._factory(endpoint)
            RABOTA_PROXY_EGRESS.labels(kind=endpoint.kind, outcome="selected").inc()
            log.info(
                "rabota_proxy_egress_selected",
                kind=endpoint.kind,
                capability=endpoint.capability,
            )

            preflight = self._preflight
            needs_preflight = (
                endpoint.kind == "free"
                and endpoint.capability == "waf_candidate"
                and preflight is not None
            )
            if not needs_preflight or preflight is None:
                return endpoint, self._active_fetcher

            try:
                await preflight(endpoint, self._active_fetcher)
            except RabotaMdWafFailClosedError as exc:
                # This is candidate validation before the caller's real request. Reject
                # the candidate, but preserve fail-closed semantics once a scan starts.
                await self._pool.report_access_rejected(endpoint)
                await self._rotate(endpoint, f"preflight_{type(exc).__name__}")
                continue
            except RabotaMdEgressError as exc:
                if exc.access_rejected:
                    await self._pool.report_access_rejected(endpoint)
                else:
                    await self._pool.report_dead(endpoint)
                await self._rotate(endpoint, f"preflight_{type(exc).__name__}")
                continue
            except httpx.TransportError as exc:
                await self._pool.report_dead(endpoint)
                await self._rotate(endpoint, f"preflight_{type(exc).__name__}")
                continue

            await self._pool.report_success(
                endpoint,
                200,
                validated_capability="full_waf",
            )
            promoted = ProxyEndpoint(
                name=endpoint.name,
                url=endpoint.url,
                kind=endpoint.kind,
                capability="full_waf",
            )
            self._active_endpoint = promoted
            log.info("rabota_proxy_preflight_ok", kind=endpoint.kind)
            return promoted, self._active_fetcher

    async def _rotate(self, endpoint: ProxyEndpoint, reason: str) -> None:
        self._excluded.add(endpoint.identity)
        self._failovers += 1
        RABOTA_PROXY_FAILOVER.labels(from_kind=endpoint.kind, reason=reason).inc()
        log.warning(
            "rabota_proxy_egress_failover",
            from_kind=endpoint.kind,
            reason=reason,
            failovers=self._failovers,
        )
        await self._close_active()
        if self._failovers > self._max_failovers:
            raise RabotaMdDegradedError(
                f"Rabota.md exhausted proxy egress failover budget ({self._max_failovers})"
            )

    async def _close_active(self) -> None:
        fetcher, self._active_fetcher = self._active_fetcher, None
        self._active_endpoint = None
        if fetcher is not None:
            await fetcher.aclose()

    async def aclose(self) -> None:
        try:
            await self._close_active()
        finally:
            await self._pool.aclose()
