from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.crawlers.adapters.delucru_md import DelucruMdAdapter
from app.crawlers.http import SecureHttpClient
from app.security.ssrf import UnsafeURLError
from app.settings import Settings


@asynccontextmanager
async def offline_socks():
    seen = []
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            assert await reader.readexactly(3) == b"\x05\x01\x00"
            writer.write(b"\x05\x00")
            await writer.drain()
            head = await reader.readexactly(4)
            assert head == b"\x05\x01\x00\x03"
            size = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(size)).decode()
            port = int.from_bytes(await reader.readexactly(2), "big")
            seen.append((host, port))
            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await writer.drain()
            request = await reader.readuntil(b"\r\n\r\n")
            assert b"GET /jobs HTTP/1.1" in request
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nfixture"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"socks5://127.0.0.1:{port}", seen
    finally:
        server.close()
        await server.wait_closed()
        if tasks:
            await asyncio.gather(*tasks)


async def public_resolver(host, port):
    return ("8.8.8.8",)


@pytest.mark.parametrize("legacy", [False, True])
async def test_shared_proxy_routes_arbitrary_future_sources_offline(monkeypatch, legacy):
    # No live crawling: all TCP connections end at an in-process SOCKS fixture.
    async with offline_socks() as (proxy, seen):
        settings = Settings(
            crawler_proxy_primary_url=None if legacy else SecretStr(proxy),
            rabota_proxy_pool_enabled=legacy,
            rabota_proxy_primary_url=SecretStr(proxy) if legacy else None,
        )
        monkeypatch.setattr("app.crawlers.http.get_settings", lambda: settings)
        for host in ("delucru.md", "rabota.md", "future-board.example"):
            client = SecureHttpClient([host], "offline-fixture", resolver=public_resolver)
            try:
                response = await client.get(f"http://{host}/jobs")
                assert response.status_code == 200 and response.text == "fixture"
                assert client._pin_resolved_addresses is False
            finally:
                await client.aclose()
        assert seen == [("delucru.md", 80), ("rabota.md", 80), ("future-board.example", 80)]


async def test_shared_proxy_does_not_override_fixture_or_explicit_transport(monkeypatch):
    settings = Settings(crawler_proxy_primary_url=SecretStr("socks5://127.0.0.1:1"))
    monkeypatch.setattr("app.crawlers.http.get_settings", lambda: settings)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="offline"))
    client = SecureHttpClient(
        ["example.com"], "fixture", resolver=public_resolver, transport=transport
    )
    try:
        assert (await client.get("https://example.com/jobs")).text == "offline"
    finally:
        await client.aclose()


async def test_shared_proxy_preserves_destination_and_redirect_guard(monkeypatch):
    settings = Settings(crawler_proxy_primary_url=SecretStr("socks5://127.0.0.1:1"))
    monkeypatch.setattr("app.crawlers.http.get_settings", lambda: settings)

    async def private_resolver(host, port):
        return ("169.254.169.254",)

    client = SecureHttpClient(["example.com"], "fixture", resolver=private_resolver)
    try:
        with pytest.raises(UnsafeURLError):
            await client.get("https://example.com/jobs")
        with pytest.raises(UnsafeURLError):
            await client.get("https://other.example/jobs")
    finally:
        await client.aclose()


async def test_delucru_inherits_shared_proxy_and_explicit_source_proxy_wins(monkeypatch):
    settings = Settings(crawler_proxy_primary_url=SecretStr("socks5://127.0.0.1:1"))
    monkeypatch.setattr("app.crawlers.http.get_settings", lambda: settings)
    for config in ({}, {"proxy_url": "socks5://127.0.0.1:2"}):
        adapter = DelucruMdAdapter(config)
        try:
            assert adapter._http._pin_resolved_addresses is False
        finally:
            await adapter.aclose()


@pytest.mark.parametrize(
    "proxy", ["socks5://missing-port", "ftp://example.com:21", "http://example.com:80/path"]
)
def test_shared_proxy_configuration_is_validated_and_redacted(proxy):
    with pytest.raises(ValidationError):
        Settings(crawler_proxy_primary_url=SecretStr(proxy))
    settings = Settings(crawler_proxy_primary_url=SecretStr("socks5://user:secret@localhost:18080"))
    assert "secret@" not in repr(settings)
