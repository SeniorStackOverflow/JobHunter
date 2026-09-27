from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.scheduler import tasks


class StubFetcher:
    def __init__(self) -> None:
        self.get_urls: list[str] = []
        self.posts: list[tuple[str, str | None]] = []
        self.closed = False

    async def get(self, url: str) -> httpx.Response:
        self.get_urls.append(url)
        return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

    async def post_html_fragment(self, url: str, *, referer: str | None = None) -> httpx.Response:
        self.posts.append((url, referer))
        return httpx.Response(200, text="fragment", request=httpx.Request("POST", url))

    async def aclose(self) -> None:
        self.closed = True


async def test_proxy_pool_probe_checks_landing_and_ajax_pagination(monkeypatch) -> None:
    fetcher = StubFetcher()
    monkeypatch.setattr(
        "app.crawlers.adapters.rabota_md.transport.build_waf_fetcher",
        lambda **_kwargs: fetcher,
    )
    source = SimpleNamespace(
        base_url="https://www.rabota.md",
        rate_limit=50,
        configuration={
            "transport": "waf_http",
            "locale_priority": ["ru"],
            "incremental_scan": {"category_slugs": ["others"]},
        },
    )

    await tasks._rabota_md_proxy_pool_probe(source, user_agent="Mozilla/5.0 Chrome/151")

    assert fetcher.get_urls == ["https://www.rabota.md/ru/"]
    assert fetcher.posts == [
        (
            "https://www.rabota.md/ru/vacancies/category/others/2",
            "https://www.rabota.md/ru/vacancies/category/others",
        )
    ]
    assert fetcher.closed is True


async def test_waf_canary_uses_proxy_pool_mode_when_enabled(monkeypatch) -> None:
    source = SimpleNamespace(
        base_url="https://www.rabota.md",
        rate_limit=50,
        enabled=True,
        configuration={"transport": "waf_http", "locale_priority": ["ru"]},
    )

    async def source_stub():
        return source

    calls: list[str] = []

    async def probe_stub(_source, *, user_agent: str) -> None:
        calls.append(user_agent)

    monkeypatch.setattr(tasks, "_rabota_md_waf_canary_source", source_stub)
    monkeypatch.setattr(tasks, "_rabota_md_proxy_pool_probe", probe_stub)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            crawler_user_agent="job-agent/test",
            rabota_proxy_pool_enabled=True,
            redis_url="redis://unused/0",
        ),
    )

    result = await tasks._rabota_md_waf_canary()

    assert result == {"outcome": "success", "mode": "proxy_pool"}
    assert len(calls) == 1
    assert calls[0].startswith("Mozilla/5.0")


async def test_proxy_reserve_maintenance_warms_before_primary_probe(monkeypatch) -> None:
    source = SimpleNamespace(
        base_url="https://www.rabota.md",
        rate_limit=50,
        enabled=True,
        configuration={
            "transport": "waf_http",
            "locale_priority": ["ru"],
            "incremental_scan": {"category_slugs": ["others"]},
        },
    )

    async def source_stub():
        return source

    warmed: list[str] = []
    probed: list[str] = []

    async def warm_stub(**kwargs):
        warmed.append(kwargs["base_url"])
        return {"ready": 3, "candidates": 2}

    async def probe_stub(_source, *, user_agent: str) -> dict[str, object]:
        probed.append(user_agent)
        return {"outcome": "success", "http_status": 200}

    monkeypatch.setattr(tasks, "_rabota_md_waf_canary_source", source_stub)
    monkeypatch.setattr(tasks, "_rabota_md_primary_egress_probe", probe_stub)
    monkeypatch.setattr(
        "app.crawlers.adapters.rabota_md.transport.warm_rabota_proxy_reserve",
        warm_stub,
    )
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            crawler_user_agent="job-agent/test",
            rabota_proxy_pool_enabled=True,
            rabota_proxy_free_fallback_enabled=True,
            rabota_proxy_target_ready_free=3,
            rabota_proxy_max_preflight_attempts=12,
            rabota_proxy_maintenance_max_preflight_attempts=6,
        ),
    )

    result = await tasks._rabota_md_proxy_reserve_maintenance()

    assert result == {
        "outcome": "ready",
        "ready": 3,
        "target_ready": 3,
        "candidates": 2,
        "primary_probe": "success",
        "consecutive_empty_cycles": 0,
        "last_ready_at": None,
    }
    assert warmed == ["https://www.rabota.md"]
    assert len(probed) == 1


@pytest.mark.parametrize(
    ("ready", "expected_outcome"),
    [
        (1, "degraded"),
        (0, "empty"),
    ],
)
async def test_proxy_reserve_maintenance_reports_capacity_state(
    monkeypatch,
    ready: int,
    expected_outcome: str,
) -> None:
    source = SimpleNamespace(
        base_url="https://www.rabota.md",
        rate_limit=50,
        enabled=True,
        configuration={
            "transport": "waf_http",
            "locale_priority": ["ru"],
            "incremental_scan": {"category_slugs": ["others"]},
        },
    )

    async def source_stub():
        return source

    async def warm_stub(**_kwargs):
        return {"ready": ready, "candidates": 4}

    async def probe_stub(_source, *, user_agent: str) -> dict[str, object]:
        del user_agent
        return {"outcome": "success", "http_status": 200}

    monkeypatch.setattr(tasks, "_rabota_md_waf_canary_source", source_stub)
    monkeypatch.setattr(tasks, "_rabota_md_primary_egress_probe", probe_stub)
    monkeypatch.setattr(
        "app.crawlers.adapters.rabota_md.transport.warm_rabota_proxy_reserve",
        warm_stub,
    )
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: SimpleNamespace(
            crawler_user_agent="job-agent/test",
            rabota_proxy_pool_enabled=True,
            rabota_proxy_free_fallback_enabled=True,
            rabota_proxy_target_ready_free=3,
            rabota_proxy_max_preflight_attempts=12,
            rabota_proxy_maintenance_max_preflight_attempts=6,
        ),
    )

    result = await tasks._rabota_md_proxy_reserve_maintenance()

    assert result == {
        "outcome": expected_outcome,
        "ready": ready,
        "target_ready": 3,
        "candidates": 4,
        "primary_probe": "success",
        "consecutive_empty_cycles": 0,
        "last_ready_at": None,
    }


async def test_proxy_reserve_health_tracks_consecutive_empty_cycles(monkeypatch) -> None:
    class FakeHealthRedis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}
            self.closed = False

        async def hset(self, _key: str, mapping: dict[str, str]) -> None:
            self.values.update(mapping)

        async def hincrby(self, _key: str, field: str, amount: int) -> int:
            value = int(self.values.get(field, "0")) + amount
            self.values[field] = str(value)
            return value

        async def hget(self, _key: str, field: str) -> str | None:
            return self.values.get(field)

        async def expire(self, _key: str, _seconds: int) -> None:
            return None

        async def aclose(self) -> None:
            self.closed = True

    fake = FakeHealthRedis()
    monkeypatch.setattr(
        "redis.asyncio.Redis.from_url",
        lambda *_args, **_kwargs: fake,
    )

    first = await tasks._record_rabota_proxy_reserve_health(
        redis_url="redis://unused/0",
        ready=0,
    )
    second = await tasks._record_rabota_proxy_reserve_health(
        redis_url="redis://unused/0",
        ready=0,
    )
    recovered = await tasks._record_rabota_proxy_reserve_health(
        redis_url="redis://unused/0",
        ready=2,
    )

    assert first == (1, None)
    assert second == (2, None)
    assert recovered[0] == 0
    assert recovered[1] is not None
    assert fake.values["consecutive_empty_cycles"] == "0"
    assert fake.values["last_ready_count"] == "2"
    assert fake.closed is True
