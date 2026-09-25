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
    RABOTA_PROXY_POOL_CANDIDATES,
    RABOTA_PROXY_POOL_READY,
)

log = structlog.get_logger()

_STATE_KEY = "crawler:rabota_md:proxy_pool:state"
_DISCOVERY_LOCK_KEY = "crawler:rabota_md:proxy_pool:discovery_lock"
_DISCOVERY_LOCK_TTL_SECONDS = 120
_FREE_PROXY_SOURCES: tuple[tuple[str, Literal["lines", "proxmint_json", "hproxy_json"]], ...] = (
    (
        "https://raw.githubusercontent.com/proxmint/free-proxy-list/main/proxies/all.json",
        "proxmint_json",
    ),
    (
        "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
        "&protocol=http&proxy_format=ipport&format=text&timeout=5000",
        "lines",
    ),
    (
        "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/all.json",
        "hproxy_json",
    ),
    ("https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt", "lines"),
    (
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/"
        "proxies/protocols/http/data.txt",
        "lines",
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

    Candidate discovery and fully-proven ready reserve state are kept separate.
    State lives in Redis so workers can maintain a hot reserve before primary failure.
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
        candidate_ttl_seconds: int = 900,
        ready_ttl_seconds: int = 1800,
        min_fresh_free: int = 5,
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
        self._candidate_ttl = candidate_ttl_seconds
        self._ready_ttl = ready_ttl_seconds
        self._min_fresh_free = min_fresh_free
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

        if not self._free_enabled:
            return None

        counts = await self.reserve_counts()
        if counts["ready"] < self._min_fresh_free and counts["candidates"] == 0:
            revalidation = await self.promotion_endpoints(excluded, limit=1)
            if not revalidation:
                await self.discover()

        endpoint = await self._known_free_endpoint(excluded)
        if endpoint is not None:
            await self._touch(endpoint)
        return endpoint

    async def report_success(
        self,
        endpoint: ProxyEndpoint,
        status_code: int,
        *,
        validated_capability: str | None = None,
    ) -> None:
        payload = await self._entry(endpoint.identity)
        capability = (
            validated_capability or endpoint.capability or payload.get("validated_capability")
        )
        status = (
            "candidate" if endpoint.kind == "free" and capability == "waf_candidate" else "ready"
        )
        payload.update(
            status=status,
            kind=endpoint.kind,
            last_check=time.time(),
            last_http_status=status_code,
            cooldown_until=0,
            fails=0,
        )
        if endpoint.kind == "free":
            payload["url"] = endpoint.url
            if capability is not None:
                payload["validated_capability"] = capability
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
            ready = await self._known_free_count()
            await self._update_reserve_metrics()
            return ready

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

            # _fetch_candidates already balances and salts each source independently.
            selected = eligible[: self._discovery_batch]
            semaphore = asyncio.Semaphore(self._validation_concurrency)
            await asyncio.gather(
                *(self._validate_free_proxy(proxy, semaphore) for proxy in selected)
            )
            counts = await self.reserve_counts()
            log.info(
                "rabota_proxy_discovery_finished",
                candidates=len(candidates),
                validated=len(selected),
                ready=counts["ready"],
                waf_candidates=counts["candidates"],
            )
            return counts["ready"]
        finally:
            await self._redis.delete(_DISCOVERY_LOCK_KEY)

    async def _fetch_candidates(self) -> list[str]:
        timeout = httpx.Timeout(15.0, connect=8.0)
        async with httpx.AsyncClient(
            timeout=timeout, trust_env=False, follow_redirects=True
        ) as client:
            responses = await asyncio.gather(
                *(self._fetch_source(client, url, kind) for url, kind in _FREE_PROXY_SOURCES)
            )

        # Balance sources so giant low-quality feeds cannot drown out smaller fresher ones.
        hour = int(time.time() // 3600)
        buckets: list[list[str]] = []
        for source_index, batch in enumerate(responses):
            ordered = sorted(
                batch,
                key=lambda proxy: hashlib.sha256(
                    f"{hour}:{source_index}:{proxy}".encode()
                ).digest(),
            )
            buckets.append(ordered)

        quota = max(10, self._discovery_batch // max(1, len(buckets)))
        selected: list[str] = []
        seen: set[str] = set()
        leftovers: list[list[str]] = []
        for bucket in buckets:
            for proxy in bucket[:quota]:
                if proxy not in seen:
                    seen.add(proxy)
                    selected.append(proxy)
            leftovers.append(bucket[quota:])

        for bucket in leftovers:
            for proxy in bucket:
                if len(selected) >= self._discovery_batch:
                    break
                if proxy not in seen:
                    seen.add(proxy)
                    selected.append(proxy)
            if len(selected) >= self._discovery_batch:
                break
        return selected

    async def _fetch_source(
        self,
        client: httpx.AsyncClient,
        url: str,
        kind: Literal["lines", "proxmint_json", "hproxy_json"],
    ) -> set[str]:
        found: set[str] = set()
        try:
            response = await client.get(url)
            response.raise_for_status()
            if kind == "lines":
                raw_candidates = response.text.splitlines()
            elif kind == "proxmint_json":
                data = response.json()
                raw_candidates = [
                    f"{item.get('ip', '')}:{item.get('port', '')}"
                    for item in data.get("proxies", [])
                    if isinstance(item, dict)
                    and item.get("protocol") == "http"
                    and self._as_int(item.get("score", 0)) >= 90
                    and self._as_int(item.get("latencyMs", 999999)) <= 1200
                ]
            else:
                data = response.json()
                raw_candidates = [
                    str(item.get("proxy", ""))
                    for item in data
                    if isinstance(item, dict)
                    and item.get("alive") is True
                    and "http" in item.get("protocols", [])
                    and self._as_float(item.get("uptime_pct", 0)) >= 90
                    and self._as_int(item.get("latency_ms", 999999)) <= 1200
                ]

            for raw in raw_candidates:
                proxy = self._normalize_public_proxy(raw)
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
                timeout = httpx.Timeout(self._validation_timeout, connect=self._validation_timeout)
                async with httpx.AsyncClient(
                    proxy=endpoint.url,
                    timeout=timeout,
                    trust_env=False,
                    follow_redirects=False,
                    headers={"User-Agent": self._user_agent},
                ) as client:
                    async with client.stream("GET", self._target_url) as response:
                        status = response.status_code
                        action = response.headers.get("x-amzn-waf-action", "").casefold()

                    if status == 202 and action == "challenge":
                        await self.report_success(
                            endpoint,
                            status,
                            validated_capability="waf_candidate",
                        )
                        return

                    if status == 200:
                        parsed = urlsplit(self._target_url)
                        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
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
                            if pagination.status_code == 202 and pagination_action == "challenge":
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

    def _fresh_candidate_state(self, payload: dict[str, object], now: float) -> bool:
        if self._as_float(payload.get("cooldown_until", 0)) > now:
            return False
        last_check = self._as_float(payload.get("last_check", 0))
        if last_check <= 0 or now - last_check > self._candidate_ttl:
            return False
        return payload.get("validated_capability") == "waf_candidate" and payload.get("status") in {
            "candidate",
            "alive",
        }

    def _fresh_ready_state(self, payload: dict[str, object], now: float) -> bool:
        if self._as_float(payload.get("cooldown_until", 0)) > now:
            return False
        last_check = self._as_float(payload.get("last_check", 0))
        if last_check <= 0 or now - last_check > self._ready_ttl:
            return False
        return payload.get("validated_capability") in {
            "direct_pagination",
            "full_waf",
        } and payload.get("status") in {"ready", "alive"}

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
            if not self._fresh_ready_state(payload, now):
                continue
            url = payload.get("url")
            if not isinstance(url, str) or not url.startswith("http://"):
                continue
            capability = payload.get("validated_capability")
            if capability not in {"direct_pagination", "full_waf"}:
                continue
            priority = {"direct_pagination": 0, "full_waf": 1}[capability]
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
        return (await self.reserve_counts())["ready"]

    async def reserve_counts(self) -> dict[str, int]:
        now = time.time()
        entries = cast(dict[object, object], await cast(Any, self._redis).hgetall(_STATE_KEY))
        ready = 0
        candidates = 0
        for raw_identity, raw_payload in entries.items():
            identity = self._decode(raw_identity)
            if not identity.startswith("free:"):
                continue
            try:
                payload = json.loads(self._decode(raw_payload))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if self._fresh_ready_state(payload, now):
                ready += 1
            elif self._fresh_candidate_state(payload, now):
                candidates += 1
        RABOTA_PROXY_POOL_ALIVE.set(ready)
        RABOTA_PROXY_POOL_READY.set(ready)
        RABOTA_PROXY_POOL_CANDIDATES.set(candidates)
        return {"ready": ready, "candidates": candidates}

    async def _update_reserve_metrics(self) -> None:
        await self.reserve_counts()

    def _revalidatable_ready_state(self, payload: dict[str, object], now: float) -> bool:
        if self._as_float(payload.get("cooldown_until", 0)) > now:
            return False
        last_check = self._as_float(payload.get("last_check", 0))
        if last_check <= 0:
            return False
        age = now - last_check
        if age <= self._ready_ttl or age > self._candidate_ttl:
            return False
        return payload.get("validated_capability") in {
            "direct_pagination",
            "full_waf",
        } and payload.get("status") in {"ready", "alive"}

    async def promotion_endpoints(
        self,
        excluded: set[str],
        *,
        limit: int,
    ) -> list[ProxyEndpoint]:
        now = time.time()
        entries = cast(dict[object, object], await cast(Any, self._redis).hgetall(_STATE_KEY))
        candidates: list[tuple[int, float, float, ProxyEndpoint]] = []
        for raw_identity, raw_payload in entries.items():
            identity = self._decode(raw_identity)
            if identity in excluded or not identity.startswith("free:"):
                continue
            try:
                payload = json.loads(self._decode(raw_payload))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            url = payload.get("url")
            if not isinstance(url, str) or not url.startswith("http://"):
                continue
            capability = payload.get("validated_capability")
            if self._revalidatable_ready_state(payload, now):
                assert capability in {"direct_pagination", "full_waf"}
                candidates.append(
                    (
                        0,
                        -self._as_float(payload.get("last_check", 0)),
                        self._as_float(payload.get("last_used", 0)),
                        ProxyEndpoint(
                            name="free",
                            url=url,
                            kind="free",
                            capability=capability,
                        ),
                    )
                )
                continue
            if not self._fresh_candidate_state(payload, now):
                continue
            candidates.append(
                (
                    1,
                    0,
                    self._as_float(payload.get("last_used", 0)),
                    ProxyEndpoint(
                        name="free",
                        url=url,
                        kind="free",
                        capability="waf_candidate",
                    ),
                )
            )
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        return [endpoint for _, _, _, endpoint in candidates[:limit]]

    async def candidate_endpoints(self, excluded: set[str], *, limit: int) -> list[ProxyEndpoint]:
        now = time.time()
        entries = cast(dict[object, object], await cast(Any, self._redis).hgetall(_STATE_KEY))
        candidates: list[tuple[float, ProxyEndpoint]] = []
        for raw_identity, raw_payload in entries.items():
            identity = self._decode(raw_identity)
            if identity in excluded or not identity.startswith("free:"):
                continue
            try:
                payload = json.loads(self._decode(raw_payload))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not self._fresh_candidate_state(payload, now):
                continue
            url = payload.get("url")
            if not isinstance(url, str) or not url.startswith("http://"):
                continue
            candidates.append(
                (
                    self._as_float(payload.get("last_used", 0)),
                    ProxyEndpoint(
                        name="free",
                        url=url,
                        kind="free",
                        capability="waf_candidate",
                    ),
                )
            )
        candidates.sort(key=lambda item: item[0])
        return [endpoint for _, endpoint in candidates[:limit]]

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
        max_preflight_attempts: int = 8,
        promotion_concurrency: int = 3,
    ) -> None:
        self._pool = pool
        self._factory = factory
        self._preflight = preflight
        self._max_failovers = max_egress_failovers
        self._max_preflight_attempts = max_preflight_attempts
        self._promotion_concurrency = promotion_concurrency
        self._active_endpoint: ProxyEndpoint | None = None
        self._active_fetcher: RabotaMdFetcher | None = None
        self._excluded: set[str] = set()
        self._failovers = 0
        self._preflight_attempts = 0
        self._emergency_discovery_attempted = False

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

        if self._failovers > self._max_failovers:
            raise RabotaMdDegradedError(
                f"Rabota.md exhausted proxy egress failover budget ({self._max_failovers})"
            )
        if self._preflight_attempts >= self._max_preflight_attempts:
            raise RabotaMdDegradedError(
                f"Rabota.md exhausted free-proxy preflight budget ({self._max_preflight_attempts})"
            )

        while True:
            endpoint = await self._pool.next_endpoint(self._excluded)
            if endpoint is None:
                remaining = self._max_preflight_attempts - self._preflight_attempts
                if remaining <= 0 or self._preflight is None:
                    raise RabotaMdDegradedError(
                        f"Rabota.md exhausted free-proxy preflight budget "
                        f"({self._max_preflight_attempts})"
                    )
                promoted_count = await self._promote_candidates(
                    remaining,
                    target_successes=1,
                )
                if promoted_count == 0:
                    if not self._emergency_discovery_attempted and remaining > 0:
                        self._emergency_discovery_attempted = True
                        await self._pool.discover()
                        continue
                    raise RabotaMdDegradedError(
                        f"Rabota.md exhausted free-proxy preflight budget "
                        f"({self._max_preflight_attempts})"
                    )
                continue
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
                await self._reject_candidate(endpoint, type(exc).__name__)
                continue
            except RabotaMdEgressError as exc:
                if exc.access_rejected:
                    await self._pool.report_access_rejected(endpoint)
                else:
                    await self._pool.report_dead(endpoint)
                await self._reject_candidate(endpoint, type(exc).__name__)
                continue
            except httpx.TransportError as exc:
                await self._pool.report_dead(endpoint)
                await self._reject_candidate(endpoint, type(exc).__name__)
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

    async def warm_reserve(self, *, target_ready: int) -> dict[str, int]:
        counts = await self._pool.reserve_counts()
        if counts["ready"] >= target_ready:
            return counts

        discovered = False
        if not await self._pool.promotion_endpoints(self._excluded, limit=1):
            await self._pool.discover()
            discovered = True

        counts = await self._pool.reserve_counts()
        needed = max(0, target_ready - counts["ready"])
        if needed and self._preflight is not None:
            await self._promote_candidates(
                self._max_preflight_attempts,
                target_successes=needed,
            )

        counts = await self._pool.reserve_counts()
        remaining_budget = max(0, self._max_preflight_attempts - self._preflight_attempts)
        if (
            counts["ready"] < target_ready
            and remaining_budget > 0
            and not discovered
            and not await self._pool.promotion_endpoints(self._excluded, limit=1)
        ):
            await self._pool.discover()
            discovered = True
            counts = await self._pool.reserve_counts()
            needed = max(0, target_ready - counts["ready"])
            if needed:
                await self._promote_candidates(
                    remaining_budget,
                    target_successes=needed,
                )

        return await self._pool.reserve_counts()

    async def _promote_candidates(
        self,
        max_candidates: int,
        *,
        target_successes: int | None = None,
    ) -> int:
        preflight = self._preflight
        if preflight is None or max_candidates <= 0:
            return 0
        candidates = await self._pool.promotion_endpoints(
            self._excluded,
            limit=max_candidates,
        )
        if not candidates:
            return 0
        semaphore = asyncio.Semaphore(self._promotion_concurrency)

        async def probe(endpoint: ProxyEndpoint) -> bool:
            async with semaphore:
                fetcher = self._factory(endpoint)
                try:
                    await preflight(endpoint, fetcher)
                except RabotaMdWafFailClosedError as exc:
                    await self._pool.report_access_rejected(endpoint)
                    reason = type(exc).__name__
                except RabotaMdEgressError as exc:
                    if exc.access_rejected:
                        await self._pool.report_access_rejected(endpoint)
                    else:
                        await self._pool.report_dead(endpoint)
                    reason = type(exc).__name__
                except httpx.TransportError as exc:
                    await self._pool.report_dead(endpoint)
                    reason = type(exc).__name__
                except Exception as exc:
                    await self._pool.report_dead(endpoint)
                    reason = type(exc).__name__
                else:
                    await self._pool.report_success(
                        endpoint,
                        200,
                        validated_capability="full_waf",
                    )
                    RABOTA_PROXY_EGRESS.labels(
                        kind=endpoint.kind,
                        outcome="promotion_success",
                    ).inc()
                    log.info("rabota_proxy_preflight_ok", kind=endpoint.kind)
                    return True
                finally:
                    await fetcher.aclose()

                self._excluded.add(endpoint.identity)
                self._preflight_attempts += 1
                RABOTA_PROXY_EGRESS.labels(
                    kind=endpoint.kind,
                    outcome="preflight_rejected",
                ).inc()
                log.warning(
                    "rabota_proxy_candidate_rejected",
                    reason=reason,
                    attempts=self._preflight_attempts,
                    max_attempts=self._max_preflight_attempts,
                )
                return False

        target = len(candidates) if target_successes is None else max(1, target_successes)
        revalidation = [
            endpoint
            for endpoint in candidates
            if endpoint.capability in {"direct_pagination", "full_waf"}
        ]
        raw_candidates = [
            endpoint for endpoint in candidates if endpoint.capability == "waf_candidate"
        ]

        async def run_batch(endpoints: list[ProxyEndpoint], needed: int) -> int:
            if not endpoints or needed <= 0:
                return 0
            tasks = [asyncio.create_task(probe(endpoint)) for endpoint in endpoints]
            successes = 0
            try:
                for completed in asyncio.as_completed(tasks):
                    if await completed:
                        successes += 1
                        if successes >= needed:
                            break
            finally:
                if successes >= needed:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            return successes

        successes = await run_batch(revalidation, target)
        if successes >= target:
            return successes
        successes += await run_batch(raw_candidates, target - successes)
        return successes

    async def _reject_candidate(self, endpoint: ProxyEndpoint, reason: str) -> None:
        self._excluded.add(endpoint.identity)
        self._preflight_attempts += 1
        RABOTA_PROXY_EGRESS.labels(kind=endpoint.kind, outcome="preflight_rejected").inc()
        log.warning(
            "rabota_proxy_candidate_rejected",
            reason=reason,
            attempts=self._preflight_attempts,
            max_attempts=self._max_preflight_attempts,
        )
        await self._close_active()
        if self._preflight_attempts >= self._max_preflight_attempts:
            raise RabotaMdDegradedError(
                f"Rabota.md exhausted free-proxy preflight budget ({self._max_preflight_attempts})"
            )

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
