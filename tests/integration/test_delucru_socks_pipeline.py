from __future__ import annotations

import asyncio
import ssl
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import SecretStr
from sqlalchemy import func, select

from app.crawlers.http import SecureHttpClient
from app.crawlers.pipeline import ScanService
from app.crawlers.registry import build_default_registry
from app.models.entities import JobSource, SourceJob
from app.models.enums import RunStatus, ScanType, SourceHealth
from app.settings import Settings
from tests.integration.test_delucru_pipeline import source_record
from tests.unit.test_delucru_adapter import BASE, default_routes, fixture


def tls_contexts(path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "www.delucru.md")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("www.delucru.md")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = path / "offline-cert.pem", path / "offline-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    client = ssl.create_default_context(cafile=str(cert_path))
    return server, client


@asynccontextmanager
async def offline_https_socks(server_tls):
    routes = default_routes()
    seen = []
    tasks = set()

    async def origin(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            path = headers.split(b" ")[1].decode()
            configured = routes.get(BASE + path, fixture("empty_listing.html"))
            status, text = configured if isinstance(configured, tuple) else (200, configured)
            body = text.encode()
            writer.write(
                (
                    f"HTTP/1.1 {status} Fixture\r\nContent-Type: text/html; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    https = await asyncio.start_server(origin, "127.0.0.1", 0, ssl=server_tls)
    tls_port = https.sockets[0].getsockname()[1]

    async def tunnel(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        upstream = None
        try:
            assert await reader.readexactly(3) == b"\x05\x01\x00"
            writer.write(b"\x05\x00")
            await writer.drain()
            assert await reader.readexactly(4) == b"\x05\x01\x00\x03"
            size = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(size)).decode()
            port = int.from_bytes(await reader.readexactly(2), "big")
            assert host == "www.delucru.md" and port == 443
            seen.append((host, port))
            remote, upstream = await asyncio.open_connection("127.0.0.1", tls_port)
            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await writer.drain()

            async def copy(source, target):
                while chunk := await source.read(32768):
                    target.write(chunk)
                    await target.drain()

            pumps = [
                asyncio.create_task(copy(reader, upstream)),
                asyncio.create_task(copy(remote, writer)),
            ]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
        finally:
            if upstream is not None:
                upstream.close()
                await upstream.wait_closed()
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    socks = await asyncio.start_server(tunnel, "127.0.0.1", 0)
    try:
        yield f"socks5://127.0.0.1:{socks.sockets[0].getsockname()[1]}", seen
    finally:
        socks.close()
        https.close()
        await socks.wait_closed()
        await https.wait_closed()
        if tasks:
            await asyncio.gather(*tasks)


async def test_delucru_full_pipeline_over_shared_socks_and_verified_tls_three_runs(
    sqlite_session_factory, tmp_path, monkeypatch
):
    # The source remains in offline mode. Real SOCKS/TLS/HTTP terminate at local
    # fixture servers; no external DNS, network, email or live crawler is enabled.
    server_tls, client_tls = tls_contexts(tmp_path)
    original_transport = httpx.AsyncHTTPTransport

    def transport(**kwargs):
        return original_transport(verify=client_tls, **kwargs)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)

    async def public_resolver(host, port):
        return ("8.8.8.8",)

    def fetcher(_):
        return SecureHttpClient(
            ["delucru.md", "www.delucru.md"],
            "offline-socks-pipeline",
            requests_per_minute=100000,
            resolver=public_resolver,
        )

    source_id = await source_record(sqlite_session_factory)
    for _ in range(3):
        async with offline_https_socks(server_tls) as (proxy, seen):
            settings = Settings(crawler_proxy_primary_url=SecretStr(proxy))
            monkeypatch.setattr(
                "app.crawlers.http.get_settings", lambda settings=settings: settings
            )
            scanner = ScanService(
                sqlite_session_factory, build_default_registry(client_factory=fetcher)
            )
            run = await scanner.run_scan((await scanner.create_scan(source_id, ScanType.FULL)).id)
            assert run.status == RunStatus.SUCCEEDED and run.found_jobs == 3
            assert len(seen) > 3
            assert all(host == "www.delucru.md" and port == 443 for host, port in seen)
        async with sqlite_session_factory() as session:
            source = await session.get(JobSource, source_id)
            assert source.health_status == SourceHealth.HEALTHY
            assert await session.scalar(select(func.count(SourceJob.id))) == 3
