from __future__ import annotations

import json

import httpx
import pytest

from app.crawlers.adapters.rabota_md.errors import (
    RabotaMdEgressError,
    RabotaMdTemporaryError,
    RabotaMdWafFailClosedError,
)
from app.crawlers.adapters.rabota_md.proxy_pool import (
    ProxyEndpoint,
    ProxyPoolFetcher,
    RabotaProxyPool,
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
    )
    primary = await pool.next_endpoint(set())
    assert primary is not None
    assert primary.kind == "primary"

    await pool.report_dead(primary)
    free = ProxyEndpoint("free", "http://1.1.1.1:8080", "free", capability="waf_candidate")
    await redis.hset(
        "crawler:rabota_md:proxy_pool:state",
        free.identity,
        json.dumps(
            {
                "status": "alive",
                "kind": "free",
                "url": free.url,
                "cooldown_until": 0,
                "last_used": 0,
                "validated_capability": "waf_candidate",
            }
        ),
    )
    selected = await pool.next_endpoint(set())
    assert selected == free


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
    )
    challenged = ProxyEndpoint("free", "http://1.1.1.1:8080", "free", capability="waf_candidate")
    direct = ProxyEndpoint("free", "http://8.8.8.8:3128", "free", capability="direct_pagination")
    state_key = "crawler:rabota_md:proxy_pool:state"
    await redis.hset(
        state_key,
        challenged.identity,
        json.dumps({
            "status": "alive", "kind": "free", "url": challenged.url,
            "cooldown_until": 0, "last_used": 0, "last_http_status": 202,
            "validated_capability": "waf_candidate",
        }),
    )
    await redis.hset(
        state_key,
        direct.identity,
        json.dumps({
            "status": "alive", "kind": "free", "url": direct.url,
            "cooldown_until": 0, "last_used": 9999, "last_http_status": 200,
            "validated_capability": "direct_pagination",
        }),
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
    assert state["status"] == "alive"
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
