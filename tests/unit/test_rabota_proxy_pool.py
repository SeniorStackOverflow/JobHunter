from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app.crawlers.adapters.rabota_md.errors import (
    RabotaMdDegradedError,
    RabotaMdEgressError,
    RabotaMdTemporaryError,
    RabotaMdWafFailClosedError,
)
from app.crawlers.adapters.rabota_md.proxy_pool import (
    ProxyEndpoint,
    ProxyPoolFetcher,
    RabotaProxyPool,
)
from app.crawlers.adapters.rabota_md.waf.errors import (
    WafChallengeRequired,
    WafSolveFailed,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.closed = False

    async def get(self, key: str):
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
        px: int | None = None,
    ):
        del ex, px
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def hget(self, name: str, key: str):
        return self.hashes.get(name, {}).get(key)

    async def hgetall(self, name: str):
        return dict(self.hashes.get(name, {}))

    async def hset(self, name: str, key: str, value: str) -> None:
        self.hashes.setdefault(name, {})[key] = value

    async def hincrby(self, name: str, key: str, amount: int) -> int:
        current = int(self.hashes.setdefault(name, {}).get(key, "0"))
        current += amount
        self.hashes[name][key] = str(current)
        return current

    async def aclose(self) -> None:
        self.closed = True


class StubFetcher:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.closed = 0

    async def get(self, url: str) -> httpx.Response:
        del url
        self.calls += 1
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def post_html_fragment(self, url: str, *, referer: str | None = None) -> httpx.Response:
        del referer
        return await self.get(url)

    async def aclose(self) -> None:
        self.closed += 1


class StubPool:
    def __init__(self, endpoints: list[ProxyEndpoint]) -> None:
        self.endpoints = list(endpoints)
        self.successes: list[str] = []
        self.dead: list[str] = []
        self.rejected: list[str] = []
        self.closed = False

    async def next_endpoint(self, excluded: set[str]) -> ProxyEndpoint | None:
        return next((item for item in self.endpoints if item.identity not in excluded), None)

    async def report_success(
        self,
        endpoint: ProxyEndpoint,
        status_code: int,
        *,
        validated_capability: str | None = None,
    ) -> None:
        del status_code, validated_capability
        self.successes.append(endpoint.identity)

    async def report_dead(self, endpoint: ProxyEndpoint) -> None:
        self.dead.append(endpoint.identity)

    async def report_rate_limited(
        self,
        endpoint: ProxyEndpoint,
        *,
        cooldown_seconds: int = 300,
    ) -> None:
        del cooldown_seconds
        self.dead.append(endpoint.identity)

    async def report_transient_failure(
        self,
        endpoint: ProxyEndpoint,
        *,
        reason: str,
        reason_class: str = "proof",
        stage: str = "unknown",
    ) -> None:
        del reason, reason_class, stage
        self.dead.append(endpoint.identity)

    async def report_access_rejected(self, endpoint: ProxyEndpoint) -> None:
        self.rejected.append(endpoint.identity)

    async def aclose(self) -> None:
        self.closed = True


def test_primary_endpoint_change_gets_new_identity_and_token_namespace() -> None:
    first = ProxyEndpoint("a14", "socks5://100.106.163.104:18080", "primary")
    moved = ProxyEndpoint("a14", "socks5://100.106.163.104:18081", "primary")

    assert first.identity != moved.identity
    assert first.token_namespace != moved.token_namespace


def test_proxy_endpoint_token_namespace_is_stable_and_egress_scoped() -> None:
    a14 = ProxyEndpoint("a14", "socks5://100.106.163.104:18080", "primary")
    free1 = ProxyEndpoint("free", "http://1.1.1.1:8080", "free")
    free2 = ProxyEndpoint("free", "http://8.8.8.8:3128", "free")

    assert a14.token_namespace == a14.token_namespace
    assert len({a14.token_namespace, free1.token_namespace, free2.token_namespace}) == 3


def test_free_proxy_parser_accepts_only_public_ip_and_explicit_port() -> None:
    assert RabotaProxyPool._normalize_public_proxy("1.1.1.1:8080") == "1.1.1.1:8080"
    assert RabotaProxyPool._normalize_public_proxy("http://8.8.8.8:3128") == "8.8.8.8:3128"
    assert RabotaProxyPool._normalize_public_proxy("127.0.0.1:8080") is None
    assert RabotaProxyPool._normalize_public_proxy("10.0.0.1:8080") is None
    assert RabotaProxyPool._normalize_public_proxy("example.com:8080") is None
    assert RabotaProxyPool._normalize_public_proxy("1.1.1.1") is None


async def test_pool_prefers_a14_then_uses_known_free_when_primary_cools_down() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url="socks5://100.106.163.104:18080",
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        min_fresh_free=1,
    )
    primary = await pool.next_endpoint(set())
    assert primary is not None
    assert primary.kind == "primary"

    await pool.report_dead(primary)
    free = ProxyEndpoint("free", "http://1.1.1.1:8080", "free", capability="full_waf")
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        free.identity,
        json.dumps(
            {
                "status": "ready",
                "kind": "free",
                "url": free.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": time.time(),
                "validated_capability": "full_waf",
            }
        ),
    )
    selected = await pool.next_endpoint(set())
    assert selected == free


async def test_pool_never_serves_unproven_waf_candidate() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        min_fresh_free=1,
    )
    candidate = ProxyEndpoint(
        "free",
        "http://1.1.1.1:8080",
        "free",
        capability="waf_candidate",
    )
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        candidate.identity,
        json.dumps(
            {
                "status": "candidate",
                "kind": "free",
                "url": candidate.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": time.time(),
                "validated_capability": "waf_candidate",
                "challenge_reachable_at": time.time(),
            }
        ),
    )

    assert await pool.next_endpoint(set()) is None
    assert await pool.candidate_endpoints(set(), limit=1) == [candidate]
    assert await pool.reserve_counts() == {"ready": 0, "candidates": 1}


async def test_fetcher_promotes_candidate_before_emergency_takeover() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        min_fresh_free=1,
    )
    candidate = ProxyEndpoint(
        "free",
        "http://1.1.1.1:8080",
        "free",
        capability="waf_candidate",
    )
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        candidate.identity,
        json.dumps(
            {
                "status": "candidate",
                "kind": "free",
                "url": candidate.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": time.time(),
                "validated_capability": "waf_candidate",
                "challenge_reachable_at": time.time(),
            }
        ),
    )
    created: list[StubFetcher] = []

    def factory(_endpoint: ProxyEndpoint) -> StubFetcher:
        fetcher = StubFetcher([httpx.Response(200, text="ready")])
        created.append(fetcher)
        return fetcher

    async def preflight(_endpoint: ProxyEndpoint, _fetcher: StubFetcher) -> None:
        return None

    fetcher = ProxyPoolFetcher(
        pool,
        factory,  # type: ignore[arg-type]
        preflight=preflight,  # type: ignore[arg-type]
        max_preflight_attempts=1,
        promotion_concurrency=1,
    )
    response = await fetcher.get("https://www.rabota.md/ru/")

    assert response.text == "ready"
    assert await pool.reserve_counts() == {"ready": 1, "candidates": 0}
    assert len(created) == 2
    assert created[0].closed == 1
    await fetcher.aclose()


async def test_emergency_promotion_stops_after_first_ready_proxy() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        min_fresh_free=1,
    )
    fast = ProxyEndpoint("free", "http://1.1.1.1:8080", "free", capability="waf_candidate")
    slow = ProxyEndpoint("free", "http://8.8.8.8:3128", "free", capability="waf_candidate")
    for endpoint in (fast, slow):
        await redis.hset(
            "crawler:rabota_md:proxy_pool:state",
            endpoint.identity,
            json.dumps(
                {
                    "status": "candidate",
                    "kind": "free",
                    "url": endpoint.url,
                    "cooldown_until": 0,
                    "last_used": 0,
                    "last_check": time.time(),
                    "validated_capability": "waf_candidate",
                    "challenge_reachable_at": time.time(),
                }
            ),
        )

    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()

    async def preflight(endpoint: ProxyEndpoint, _fetcher: StubFetcher) -> None:
        if endpoint.identity == fast.identity:
            await slow_started.wait()
            return
        slow_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            slow_cancelled.set()
            raise

    fetcher = ProxyPoolFetcher(
        pool,
        lambda _endpoint: StubFetcher([]),  # type: ignore[arg-type]
        preflight=preflight,  # type: ignore[arg-type]
        max_preflight_attempts=2,
        promotion_concurrency=2,
    )

    promoted = await fetcher._promote_candidates(2, target_successes=1)

    assert promoted == 1
    assert slow_cancelled.is_set()
    assert await pool.reserve_counts() == {"ready": 1, "candidates": 1}
    await fetcher.aclose()


async def test_warm_reserve_promotes_candidates_while_primary_is_healthy() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url="socks5://100.106.163.104:18080",
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        min_fresh_free=1,
    )
    candidate = ProxyEndpoint(
        "free",
        "http://1.1.1.1:8080",
        "free",
        capability="waf_candidate",
    )
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        candidate.identity,
        json.dumps(
            {
                "status": "candidate",
                "kind": "free",
                "url": candidate.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": time.time(),
                "validated_capability": "waf_candidate",
                "challenge_reachable_at": time.time(),
            }
        ),
    )
    created: list[StubFetcher] = []

    def factory(_endpoint: ProxyEndpoint) -> StubFetcher:
        fetcher = StubFetcher([])
        created.append(fetcher)
        return fetcher

    async def preflight(_endpoint: ProxyEndpoint, _fetcher: StubFetcher) -> None:
        return None

    fetcher = ProxyPoolFetcher(
        pool,
        factory,  # type: ignore[arg-type]
        preflight=preflight,  # type: ignore[arg-type]
        max_preflight_attempts=2,
        promotion_concurrency=1,
    )
    counts = await fetcher.warm_reserve(target_ready=1)

    assert counts == {"ready": 1, "candidates": 0}
    assert len(created) == 1
    assert created[0].closed == 1
    await fetcher.aclose()


async def test_warm_reserve_revalidates_stale_ready_before_new_candidate() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url="socks5://100.106.163.104:18080",
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        candidate_ttl_seconds=900,
        ready_ttl_seconds=300,
        min_fresh_free=1,
    )
    stale = ProxyEndpoint(
        "free",
        "http://1.1.1.1:8080",
        "free",
        capability="full_waf",
    )
    fresh = ProxyEndpoint(
        "free",
        "http://8.8.8.8:3128",
        "free",
        capability="waf_candidate",
    )
    now = time.time()
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        stale.identity,
        json.dumps(
            {
                "status": "ready",
                "kind": "free",
                "url": stale.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": now - 500,
                "validated_capability": "full_waf",
            }
        ),
    )
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        fresh.identity,
        json.dumps(
            {
                "status": "candidate",
                "kind": "free",
                "url": fresh.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": now,
                "validated_capability": "waf_candidate",
                "challenge_reachable_at": time.time(),
            }
        ),
    )
    seen: list[str] = []

    async def preflight(endpoint: ProxyEndpoint, _fetcher: StubFetcher) -> None:
        seen.append(endpoint.identity)

    fetcher = ProxyPoolFetcher(
        pool,
        lambda _endpoint: StubFetcher([]),  # type: ignore[arg-type]
        preflight=preflight,  # type: ignore[arg-type]
        max_preflight_attempts=2,
        promotion_concurrency=1,
    )

    counts = await fetcher.warm_reserve(target_ready=1)

    assert seen == [stale.identity]
    assert counts == {"ready": 1, "candidates": 1}
    await fetcher.aclose()


async def test_warm_reserve_refills_after_stale_revalidation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url="socks5://100.106.163.104:18080",
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        candidate_ttl_seconds=900,
        ready_ttl_seconds=300,
        min_fresh_free=1,
    )
    stale = ProxyEndpoint(
        "free",
        "http://1.1.1.1:8080",
        "free",
        capability="full_waf",
    )
    fresh = ProxyEndpoint(
        "free",
        "http://8.8.8.8:3128",
        "free",
        capability="waf_candidate",
    )
    now = time.time()
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        stale.identity,
        json.dumps(
            {
                "status": "ready",
                "kind": "free",
                "url": stale.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": now - 500,
                "validated_capability": "full_waf",
            }
        ),
    )
    discoveries = 0

    async def discover() -> int:
        nonlocal discoveries
        discoveries += 1
        await redis.hset(
            "crawler:rabota_md:proxy_pool:state",
            fresh.identity,
            json.dumps(
                {
                    "status": "candidate",
                    "kind": "free",
                    "url": fresh.url,
                    "cooldown_until": 0,
                    "last_used": 0,
                    "last_check": time.time(),
                    "validated_capability": "waf_candidate",
                    "challenge_reachable_at": time.time(),
                }
            ),
        )
        return 0

    monkeypatch.setattr(pool, "discover", discover)
    seen: list[str] = []

    async def preflight(endpoint: ProxyEndpoint, _fetcher: StubFetcher) -> None:
        seen.append(endpoint.identity)
        if endpoint.identity == stale.identity:
            raise RabotaMdEgressError("known proxy expired")

    fetcher = ProxyPoolFetcher(
        pool,
        lambda _endpoint: StubFetcher([]),  # type: ignore[arg-type]
        preflight=preflight,  # type: ignore[arg-type]
        max_preflight_attempts=2,
        promotion_concurrency=1,
    )

    counts = await fetcher.warm_reserve(target_ready=1)

    assert discoveries == 1
    assert seen == [stale.identity, fresh.identity]
    assert counts == {"ready": 1, "candidates": 0}
    await fetcher.aclose()


async def test_fetcher_fails_over_from_a14_and_stays_on_free_proxy() -> None:
    a14 = ProxyEndpoint("a14", "socks5://100.106.163.104:18080", "primary")
    free = ProxyEndpoint("free", "http://1.1.1.1:8080", "free", capability="waf_candidate")
    pool = StubPool([a14, free])
    a14_fetcher = StubFetcher([RabotaMdEgressError("a14 down")])
    free_fetcher = StubFetcher(
        [httpx.Response(200, text="free-1"), httpx.Response(200, text="free-2")]
    )

    def factory(endpoint: ProxyEndpoint):
        return a14_fetcher if endpoint.kind == "primary" else free_fetcher

    fetcher = ProxyPoolFetcher(pool, factory)  # type: ignore[arg-type]
    first = await fetcher.get("https://www.rabota.md/ru/")
    second = await fetcher.get("https://www.rabota.md/ru/vacancies")

    assert first.text == "free-1"
    assert second.text == "free-2"
    assert a14_fetcher.calls == 1
    assert a14_fetcher.closed == 1
    assert free_fetcher.calls == 2
    assert pool.dead == [a14.identity]
    assert pool.successes == [free.identity, free.identity]


async def test_access_rejection_marks_egress_banned_before_failover() -> None:
    a14 = ProxyEndpoint("a14", "socks5://100.106.163.104:18080", "primary")
    free = ProxyEndpoint("free", "http://1.1.1.1:8080", "free", capability="waf_candidate")
    pool = StubPool([a14, free])
    a14_fetcher = StubFetcher([RabotaMdEgressError("bare 403", access_rejected=True)])
    free_fetcher = StubFetcher([httpx.Response(200, text="ok")])
    fetcher = ProxyPoolFetcher(
        pool,  # type: ignore[arg-type]
        lambda endpoint: a14_fetcher if endpoint.kind == "primary" else free_fetcher,
    )

    assert (await fetcher.get("https://www.rabota.md/ru/")).status_code == 200
    assert pool.rejected == [a14.identity]
    assert pool.dead == []


async def test_captcha_is_fail_closed_and_never_rotates_egress() -> None:
    a14 = ProxyEndpoint("a14", "socks5://100.106.163.104:18080", "primary")
    free = ProxyEndpoint("free", "http://1.1.1.1:8080", "free", capability="waf_candidate")
    pool = StubPool([a14, free])
    a14_fetcher = StubFetcher([RabotaMdWafFailClosedError("CAPTCHA")])
    free_fetcher = StubFetcher([httpx.Response(200)])
    fetcher = ProxyPoolFetcher(
        pool,  # type: ignore[arg-type]
        lambda endpoint: a14_fetcher if endpoint.kind == "primary" else free_fetcher,
    )

    with pytest.raises(RabotaMdWafFailClosedError):
        await fetcher.get("https://www.rabota.md/ru/")
    assert free_fetcher.calls == 0
    assert pool.dead == []
    assert pool.rejected == []


async def test_target_rate_limit_is_not_identity_rotation() -> None:
    a14 = ProxyEndpoint("a14", "socks5://100.106.163.104:18080", "primary")
    free = ProxyEndpoint("free", "http://1.1.1.1:8080", "free", capability="waf_candidate")
    pool = StubPool([a14, free])
    a14_fetcher = StubFetcher([RabotaMdTemporaryError("429")])
    free_fetcher = StubFetcher([httpx.Response(200)])
    fetcher = ProxyPoolFetcher(
        pool,  # type: ignore[arg-type]
        lambda endpoint: a14_fetcher if endpoint.kind == "primary" else free_fetcher,
    )

    with pytest.raises(RabotaMdTemporaryError):
        await fetcher.get("https://www.rabota.md/ru/")
    assert free_fetcher.calls == 0


async def test_pool_prefers_direct_200_free_proxy_over_waf_candidate() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        min_fresh_free=1,
    )
    challenged = ProxyEndpoint("free", "http://1.1.1.1:8080", "free", capability="waf_candidate")
    direct = ProxyEndpoint("free", "http://8.8.8.8:3128", "free", capability="direct_pagination")
    state_key = "crawler:rabota_md:proxy_pool:state"
    await redis.hset(
        state_key,
        challenged.identity,
        json.dumps(
            {
                "status": "alive",
                "kind": "free",
                "url": challenged.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": time.time(),
                "last_http_status": 202,
                "validated_capability": "waf_candidate",
                "challenge_reachable_at": time.time(),
            }
        ),
    )
    await redis.hset(
        state_key,
        direct.identity,
        json.dumps(
            {
                "status": "alive",
                "kind": "free",
                "url": direct.url,
                "cooldown_until": 0,
                "last_used": 9999,
                "last_check": time.time(),
                "last_http_status": 200,
                "validated_capability": "direct_pagination",
            }
        ),
    )

    selected = await pool.next_endpoint(set())
    assert selected == direct


async def test_direct_200_validation_keeps_client_open_for_ajax_post(monkeypatch) -> None:
    class FakeStream:
        def __init__(self, client) -> None:
            self.client = client

        async def __aenter__(self) -> httpx.Response:
            assert not self.client.closed
            return httpx.Response(200, text="landing")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            self.closed = True

        def stream(self, method: str, url: str) -> FakeStream:
            assert method == "GET"
            return FakeStream(self)

        async def post(self, url: str, **kwargs) -> httpx.Response:
            assert not self.closed, "validator closed AsyncClient before AJAX capability probe"
            return httpx.Response(
                200,
                json={"success": True, "data": {"content": "<div>jobs</div>"}},
            )

    monkeypatch.setattr(
        "app.crawlers.adapters.rabota_md.proxy_pool.httpx.AsyncClient",
        FakeAsyncClient,
    )
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
    )

    await pool._validate_free_proxy("1.1.1.1:8080", __import__("asyncio").Semaphore(1))

    endpoint = ProxyEndpoint("free", "http://1.1.1.1:8080", "free")
    raw = await redis.hget("crawler:rabota_md:proxy_pool:state", endpoint.identity)
    assert raw is not None
    state = json.loads(raw)
    assert state["status"] == "ready"
    assert state["validated_capability"] == "direct_pagination"


async def test_waf_candidate_runs_preflight_then_stays_on_promoted_egress() -> None:
    candidate = ProxyEndpoint(
        "free",
        "http://1.1.1.1:8080",
        "free",
        capability="waf_candidate",
    )
    pool = StubPool([candidate])
    fetcher_impl = StubFetcher([httpx.Response(200, text="ok")])
    seen: list[str] = []

    async def preflight(endpoint: ProxyEndpoint, fetcher) -> None:
        assert fetcher is fetcher_impl
        seen.append(endpoint.identity)

    fetcher = ProxyPoolFetcher(
        pool,  # type: ignore[arg-type]
        lambda _endpoint: fetcher_impl,
        preflight=preflight,
    )
    response = await fetcher.get("https://www.rabota.md/ru/")

    assert response.status_code == 200
    assert seen == [candidate.identity]
    assert fetcher_impl.calls == 1


async def test_failed_candidate_preflight_rotates_before_real_request() -> None:
    candidate = ProxyEndpoint(
        "free",
        "http://1.1.1.1:8080",
        "free",
        capability="waf_candidate",
    )
    direct = ProxyEndpoint(
        "free",
        "http://8.8.8.8:3128",
        "free",
        capability="direct_pagination",
    )
    pool = StubPool([candidate, direct])
    candidate_fetcher = StubFetcher([httpx.Response(200, text="must-not-run")])
    direct_fetcher = StubFetcher([httpx.Response(200, text="ok")])

    async def preflight(endpoint: ProxyEndpoint, _fetcher) -> None:
        if endpoint.identity == candidate.identity:
            raise RabotaMdEgressError("candidate proof failed")

    fetcher = ProxyPoolFetcher(
        pool,  # type: ignore[arg-type]
        lambda endpoint: (
            candidate_fetcher if endpoint.identity == candidate.identity else direct_fetcher
        ),
        preflight=preflight,
    )
    response = await fetcher.get("https://www.rabota.md/ru/")

    assert response.text == "ok"
    assert candidate_fetcher.calls == 0
    assert direct_fetcher.calls == 1
    assert pool.dead == [candidate.identity]


async def test_stale_free_proxy_is_not_selected() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        free_fallback_enabled=False,
        candidate_ttl_seconds=60,
    )
    stale = ProxyEndpoint("free", "http://1.1.1.1:8080", "free", capability="waf_candidate")
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        stale.identity,
        json.dumps(
            {
                "status": "alive",
                "kind": "free",
                "url": stale.url,
                "cooldown_until": 0,
                "last_check": time.time() - 120,
                "validated_capability": "waf_candidate",
                "challenge_reachable_at": time.time(),
            }
        ),
    )

    assert await pool._known_free_endpoint(set()) is None


async def test_candidate_preflight_failures_do_not_consume_egress_failover_budget() -> None:
    first = ProxyEndpoint("free", "http://1.1.1.1:8080", "free", capability="waf_candidate")
    second = ProxyEndpoint("free", "http://8.8.8.8:3128", "free", capability="waf_candidate")
    pool = StubPool([first, second])
    first_fetcher = StubFetcher([])
    second_fetcher = StubFetcher([httpx.Response(200, text="ok")])

    async def preflight(endpoint: ProxyEndpoint, _fetcher) -> None:
        if endpoint.identity == first.identity:
            raise RabotaMdEgressError("candidate proof failed")

    fetcher = ProxyPoolFetcher(
        pool,  # type: ignore[arg-type]
        lambda endpoint: first_fetcher if endpoint.identity == first.identity else second_fetcher,
        preflight=preflight,
        max_egress_failovers=0,
        max_preflight_attempts=2,
    )

    assert (await fetcher.get("https://www.rabota.md/ru/")).status_code == 200
    assert pool.dead == [first.identity]


async def test_preflight_budget_stays_exhausted_after_caller_catches_error() -> None:
    endpoints = [
        ProxyEndpoint(
            "free",
            f"http://1.1.1.{index}:8080",
            "free",
            capability="waf_candidate",
        )
        for index in range(1, 4)
    ]
    pool = StubPool(endpoints)
    fetchers = {endpoint.identity: StubFetcher([]) for endpoint in endpoints}

    async def preflight(_endpoint: ProxyEndpoint, _fetcher) -> None:
        raise RabotaMdEgressError("candidate proof failed")

    fetcher = ProxyPoolFetcher(
        pool,  # type: ignore[arg-type]
        lambda endpoint: fetchers[endpoint.identity],
        preflight=preflight,
        max_preflight_attempts=2,
    )

    with pytest.raises(RabotaMdDegradedError):
        await fetcher.get("https://www.rabota.md/ru/")
    with pytest.raises(RabotaMdDegradedError):
        await fetcher.get("https://www.rabota.md/ru/")


async def test_ready_proxy_stays_fresh_across_one_maintenance_interval() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        ready_ttl_seconds=1800,
        min_fresh_free=1,
    )
    endpoint = ProxyEndpoint(
        "free",
        "http://1.1.1.1:8080",
        "free",
        capability="full_waf",
    )
    now = time.time()
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        endpoint.identity,
        json.dumps(
            {
                "status": "ready",
                "kind": "free",
                "url": endpoint.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": now - 901,
                "last_success_at": now - 901,
                "validated_capability": "full_waf",
            }
        ),
    )

    assert await pool.reserve_counts() == {"ready": 1, "candidates": 0}
    await pool.aclose()


async def test_transient_revalidation_failure_is_suspect_before_dead() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        ready_ttl_seconds=1800,
        revalidation_grace_seconds=3600,
        revalidation_retry_seconds=300,
        revalidation_max_failures=3,
        dead_cooldown_seconds=3600,
        min_fresh_free=1,
    )
    endpoint = ProxyEndpoint(
        "free",
        "http://1.1.1.1:8080",
        "free",
        capability="full_waf",
    )
    now = time.time()
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        endpoint.identity,
        json.dumps(
            {
                "status": "ready",
                "kind": "free",
                "url": endpoint.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": now - 1900,
                "last_success_at": now - 1900,
                "validated_capability": "full_waf",
            }
        ),
    )

    await pool.report_transient_failure(endpoint, reason="WafSolveFailed")
    first = json.loads(redis.hashes["crawler:rabota_md:proxy_pool:state"][endpoint.identity])
    assert first["status"] == "suspect"
    assert first["revalidation_failures"] == 1
    assert first["validated_capability"] == "full_waf"
    assert first["cooldown_until"] > time.time()
    assert await pool.reserve_counts() == {"ready": 0, "candidates": 0}

    await pool.report_transient_failure(endpoint, reason="WafSolveFailed")
    second = json.loads(redis.hashes["crawler:rabota_md:proxy_pool:state"][endpoint.identity])
    assert second["status"] == "suspect"
    assert second["revalidation_failures"] == 2

    await pool.report_transient_failure(endpoint, reason="WafSolveFailed")
    third = json.loads(redis.hashes["crawler:rabota_md:proxy_pool:state"][endpoint.identity])
    assert third["status"] == "dead"
    assert third["revalidation_failures"] == 3
    assert third["cooldown_until"] > time.time() + 3500
    await pool.aclose()


async def test_waf_candidate_solve_failure_remains_retryable() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        revalidation_retry_seconds=300,
        revalidation_max_failures=3,
        min_fresh_free=1,
    )
    candidate = ProxyEndpoint(
        "free",
        "http://8.8.8.8:3128",
        "free",
        capability="waf_candidate",
    )
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        candidate.identity,
        json.dumps(
            {
                "status": "candidate",
                "kind": "free",
                "url": candidate.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": time.time(),
                "validated_capability": "waf_candidate",
                "challenge_reachable_at": time.time(),
            }
        ),
    )

    async def preflight(_endpoint: ProxyEndpoint, _fetcher: StubFetcher) -> None:
        raise WafSolveFailed("temporary proof failure")

    fetcher = ProxyPoolFetcher(
        pool,
        lambda _endpoint: StubFetcher([]),  # type: ignore[arg-type]
        preflight=preflight,  # type: ignore[arg-type]
        max_preflight_attempts=1,
        promotion_concurrency=1,
    )
    counts = await fetcher.warm_reserve(target_ready=1)

    stored = json.loads(redis.hashes["crawler:rabota_md:proxy_pool:state"][candidate.identity])
    assert counts == {"ready": 0, "candidates": 0}
    assert stored["status"] == "candidate"
    assert stored["proof_failures"] == 1
    assert stored["cooldown_until"] > time.time()
    await fetcher.aclose()


async def test_challenged_proxy_requires_reachable_challenge_assets(monkeypatch) -> None:
    class FakeStream:
        async def __aenter__(self) -> httpx.Response:
            return httpx.Response(
                202,
                headers={"x-amzn-waf-action": "challenge"},
                text="challenge",
            )

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method: str, url: str) -> FakeStream:
            del method, url
            return FakeStream()

    seen: list[str] = []

    async def probe(self, site: str, user_agent: str) -> str:
        del self, user_agent
        seen.append(site)
        return "abc123"

    monkeypatch.setattr(
        "app.crawlers.adapters.rabota_md.proxy_pool.httpx.AsyncClient",
        FakeAsyncClient,
    )
    monkeypatch.setattr(
        "app.crawlers.adapters.rabota_md.proxy_pool.AwsWafSolver.probe_challenge",
        probe,
    )
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
    )

    await pool._validate_free_proxy(
        "1.1.1.1:8080",
        asyncio.Semaphore(1),
    )

    endpoint = ProxyEndpoint("free", "http://1.1.1.1:8080", "free")
    state = json.loads(redis.hashes["crawler:rabota_md:proxy_pool:state"][endpoint.identity])
    assert state["validated_capability"] == "waf_candidate"
    assert state["status"] == "candidate"
    assert seen == ["https://www.rabota.md"]


async def test_challenged_proxy_with_unreachable_script_is_not_candidate(monkeypatch) -> None:
    class FakeStream:
        async def __aenter__(self) -> httpx.Response:
            return httpx.Response(
                202,
                headers={"x-amzn-waf-action": "challenge"},
                text="challenge",
            )

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method: str, url: str) -> FakeStream:
            del method, url
            return FakeStream()

    async def probe(self, site: str, user_agent: str) -> str:
        del self, site, user_agent
        from app.crawlers.adapters.rabota_md.waf.errors import WafTransportError

        raise WafTransportError(stage="challenge_script", error_type="ConnectTimeout")

    monkeypatch.setattr(
        "app.crawlers.adapters.rabota_md.proxy_pool.httpx.AsyncClient",
        FakeAsyncClient,
    )
    monkeypatch.setattr(
        "app.crawlers.adapters.rabota_md.proxy_pool.AwsWafSolver.probe_challenge",
        probe,
    )
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
    )

    await pool._validate_free_proxy(
        "1.1.1.1:8080",
        asyncio.Semaphore(1),
    )

    endpoint = ProxyEndpoint("free", "http://1.1.1.1:8080", "free")
    state = json.loads(redis.hashes["crawler:rabota_md:proxy_pool:state"][endpoint.identity])
    assert state["status"] == "dead"
    assert state.get("validated_capability") != "waf_candidate"


async def test_free_proxy_http_429_uses_short_rate_limit_cooldown() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
    )
    endpoint = ProxyEndpoint("free", "http://1.1.1.1:8080", "free")
    before = time.time()

    await pool.report_rate_limited(endpoint, cooldown_seconds=300)

    state = json.loads(redis.hashes["crawler:rabota_md:proxy_pool:state"][endpoint.identity])
    assert state["status"] == "rate_limited"
    assert state["last_http_status"] == 429
    assert before + 295 <= state["cooldown_until"] <= time.time() + 305


async def test_legacy_waf_candidate_without_challenge_marker_is_not_promoted() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
    )
    endpoint = ProxyEndpoint(
        "free",
        "http://1.1.1.1:8080",
        "free",
        capability="waf_candidate",
    )
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        endpoint.identity,
        json.dumps(
            {
                "status": "candidate",
                "kind": "free",
                "url": endpoint.url,
                "cooldown_until": 0,
                "last_check": time.time(),
                "validated_capability": "waf_candidate",
            }
        ),
    )

    assert await pool.promotion_endpoints(set(), limit=3) == []


def test_waf_challenge_required_has_explicit_preflight_taxonomy() -> None:
    exc = WafChallengeRequired(
        "fresh token still challenged",
        reason_code="challenge_persisted",
    )

    assert ProxyPoolFetcher._failure_details(exc) == (
        "challenge_persisted",
        "proof",
        "post_refresh",
    )


async def test_waf_challenge_required_keeps_free_candidate_retryable() -> None:
    redis = FakeRedis()
    pool = RabotaProxyPool(
        redis,  # type: ignore[arg-type]
        primary_url=None,
        target_url="https://www.rabota.md/ru/",
        user_agent="Mozilla/5.0 Chrome/151",
        revalidation_retry_seconds=300,
        revalidation_max_failures=3,
        min_fresh_free=1,
    )
    candidate = ProxyEndpoint(
        "free",
        "http://8.8.8.8:3128",
        "free",
        capability="waf_candidate",
    )
    now = time.time()
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        candidate.identity,
        json.dumps(
            {
                "status": "candidate",
                "kind": "free",
                "url": candidate.url,
                "cooldown_until": 0,
                "last_used": 0,
                "last_check": now,
                "challenge_reachable_at": now,
                "validated_capability": "waf_candidate",
            }
        ),
    )

    async def preflight(_endpoint: ProxyEndpoint, _fetcher: StubFetcher) -> None:
        raise WafChallengeRequired(
            "fresh token still challenged",
            reason_code="challenge_persisted",
        )

    fetcher = ProxyPoolFetcher(
        pool,
        lambda _endpoint: StubFetcher([]),  # type: ignore[arg-type]
        preflight=preflight,  # type: ignore[arg-type]
        max_preflight_attempts=1,
        promotion_concurrency=1,
    )

    counts = await fetcher.warm_reserve(target_ready=1)

    state = json.loads(redis.hashes["crawler:rabota_md:proxy_pool:state"][candidate.identity])
    assert counts == {"ready": 0, "candidates": 0}
    assert state["status"] == "candidate"
    assert state["proof_failures"] == 1
    assert state["last_failure_class"] == "proof"
    assert state["last_failure_stage"] == "post_refresh"
    assert state["last_revalidation_error"] == "challenge_persisted"
    await fetcher.aclose()
