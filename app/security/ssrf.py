from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit


class UnsafeURLError(ValueError):
    """Raised before an outbound request to an unsafe destination."""


Resolver = Callable[[str, int], Awaitable[Iterable[str]]]


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    hostname: str
    addresses: tuple[str, ...]


BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}
BLOCKED_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}
ALLOWED_OUTBOUND_PORTS = frozenset({80, 443})
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:^|[-_])(?:access|refresh)?token(?:$|[-_])|api[-_]?key|authorization|"
    r"client[-_]?secret|password|passwd|credential|signature|oauth|secret",
    re.IGNORECASE,
)


def _normalized_hostname(hostname: str) -> str:
    return hostname.rstrip(".").encode("idna").decode("ascii").lower()


def _domain_allowed(hostname: str, allowed_domains: Iterable[str]) -> bool:
    normalized = _normalized_hostname(hostname)
    for domain in allowed_domains:
        candidate = _normalized_hostname(domain)
        if normalized == candidate or normalized.endswith(f".{candidate}"):
            return True
    return False


def _address_is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", maxsplit=1)[0])
    if ip in BLOCKED_METADATA_IPS:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_configured_url_shape(url: str, *, allow_query: bool = True) -> None:
    """Reject credentials and secret-bearing query parameters before persistence."""

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURLError("configured URL must use HTTP(S) and include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("userinfo in configured URLs is forbidden")
    if not allow_query and parsed.query:
        raise UnsafeURLError("query parameters are forbidden in a source base URL")
    if any(_SENSITIVE_QUERY_KEY.search(key) for key, _value in parse_qsl(parsed.query)):
        raise UnsafeURLError("credentials must not be placed in configured URL queries")


def public_url_shape_is_safe(url: str, allowed_domains: Iterable[str]) -> bool:
    """Synchronous gate for discovered/stored links; network fetches add DNS validation."""

    try:
        validate_configured_url_shape(url)
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        hostname = _normalized_hostname(parsed.hostname or "")
    except (UnsafeURLError, UnicodeError, ValueError):
        return False
    if port not in ALLOWED_OUTBOUND_PORTS:
        return False
    if hostname in BLOCKED_HOSTNAMES or hostname.endswith((".local", ".internal", ".localhost")):
        return False
    if not _domain_allowed(hostname, allowed_domains):
        return False
    try:
        return _address_is_public(hostname)
    except ValueError:
        return True


async def system_resolver(hostname: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return tuple(sorted({str(record[4][0]) for record in records}))


async def validate_outbound_url(
    url: str,
    allowed_domains: Iterable[str],
    resolver: Resolver | None = None,
) -> ValidatedURL:
    validate_configured_url_shape(url)
    parsed = urlsplit(url)
    assert parsed.hostname is not None

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError("URL has an invalid port") from exc
    if port not in ALLOWED_OUTBOUND_PORTS:
        raise UnsafeURLError("outbound URL port is not allowed")
    if (parsed.scheme == "https" and port != 443) or (parsed.scheme == "http" and port != 80):
        raise UnsafeURLError("outbound URL port does not match its scheme")

    hostname = _normalized_hostname(parsed.hostname)
    if hostname in BLOCKED_HOSTNAMES or hostname.endswith((".local", ".internal", ".localhost")):
        raise UnsafeURLError("internal hostname is forbidden")
    if not _domain_allowed(hostname, allowed_domains):
        raise UnsafeURLError("hostname is not in the source allowlist")

    addresses: tuple[str, ...]
    try:
        literal_ip = ipaddress.ip_address(hostname)
        addresses = (str(literal_ip),)
    except ValueError:
        resolve = resolver or system_resolver
        addresses = tuple(await resolve(hostname, port))
    if not addresses:
        raise UnsafeURLError("hostname did not resolve")
    if any(not _address_is_public(address) for address in addresses):
        raise UnsafeURLError("hostname resolves to a non-public address")

    normalized_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
    )
    return ValidatedURL(url=normalized_url, hostname=hostname, addresses=addresses)


def preferred_public_address(addresses: Iterable[str]) -> str:
    """Choose a checked address deterministically, preferring IPv4 for compatibility."""

    parsed = [ipaddress.ip_address(value.split("%", maxsplit=1)[0]) for value in addresses]
    if not parsed or any(not _address_is_public(str(value)) for value in parsed):
        raise UnsafeURLError("no validated public address is available")
    return str(sorted(parsed, key=lambda value: (value.version, int(value)))[0])


async def validate_redirect(
    current_url: str,
    location: str,
    allowed_domains: Iterable[str],
    resolver: Resolver | None = None,
) -> ValidatedURL:
    return await validate_outbound_url(urljoin(current_url, location), allowed_domains, resolver)
