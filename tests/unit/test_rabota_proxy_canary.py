from __future__ import annotations

from types import SimpleNamespace

import httpx

from app.scheduler import tasks


class StubFetcher:
    def __init__(self) -> None:
        self.get_urls: list[str] = []
        self.posts: list[tuple[str, str | None]] = []
        self.closed = False

    async def get(self, url: str) -> httpx.Response:
        self.get_urls.append(url)
        return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

    async def post_html_fragment(
        self, url: str, *, referer: str | None = None
    ) -> httpx.Response:
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
