"""Bounded DNS preflight for recipient mail routing.

Syntax validation (``validate_public_email``) does not prove that a domain can
receive mail. Before a provider submission the sender asks whether the domain
has a usable mail route:

* NXDOMAIN, a null MX (RFC 7505) or neither MX nor A/AAAA records are
  permanent routing failures and block the submission;
* timeout, SERVFAIL or an unreachable resolver are temporary: the attempt is
  postponed and the contact is not treated as permanently rejected;
* a domain without MX but with A/AAAA records is routable through the RFC 5321
  implicit MX fallback.

A routable domain does not prove that the mailbox exists. Results are cached
per process with a TTL so a batch does not repeat lookups.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class MailRoutingStatus(StrEnum):
    ROUTABLE = "routable"
    NO_DOMAIN = "no_domain"
    NULL_MX = "null_mx"
    NO_MAIL_ROUTING = "no_mail_routing"
    TEMPORARY_FAILURE = "temporary_failure"


PERMANENT_ROUTING_FAILURES = frozenset(
    {
        MailRoutingStatus.NO_DOMAIN,
        MailRoutingStatus.NULL_MX,
        MailRoutingStatus.NO_MAIL_ROUTING,
    }
)


@dataclass(frozen=True)
class MailRoutingResult:
    domain: str
    status: MailRoutingStatus
    detail: str

    @property
    def permanent_failure(self) -> bool:
        return self.status in PERMANENT_ROUTING_FAILURES

    @property
    def temporary_failure(self) -> bool:
        return self.status is MailRoutingStatus.TEMPORARY_FAILURE


class DnsNameNotFound(Exception):
    """The queried name does not exist (NXDOMAIN)."""


class DnsNoAnswer(Exception):
    """The name exists but has no records of the requested type."""


class DnsTemporaryError(Exception):
    """Timeout, SERVFAIL, refused or unreachable resolver."""


class DnsLookup(Protocol):
    async def query(self, name: str, rdtype: str) -> list[str]:
        """Return record texts; MX records are returned as the exchange name."""
        ...


class DnspythonLookup:
    """Production lookup backed by dnspython's asyncio resolver."""

    def __init__(self, *, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds

    async def query(self, name: str, rdtype: str) -> list[str]:
        import dns.asyncresolver
        import dns.exception
        import dns.resolver

        try:
            answer = await dns.asyncresolver.resolve(
                name,
                rdtype,
                lifetime=self.timeout_seconds,
                search=False,
            )
        except dns.resolver.NXDOMAIN as exc:
            raise DnsNameNotFound(name) from exc
        except dns.resolver.NoAnswer as exc:
            raise DnsNoAnswer(name) from exc
        except (
            dns.resolver.NoNameservers,
            dns.resolver.LifetimeTimeout,
            dns.exception.Timeout,
            dns.resolver.YXDOMAIN,
        ) as exc:
            raise DnsTemporaryError(type(exc).__name__) from exc
        except dns.exception.DNSException as exc:
            raise DnsTemporaryError(type(exc).__name__) from exc
        if rdtype == "MX":
            return [record.exchange.to_text() for record in answer]
        return [record.to_text() for record in answer]


class MailRoutingChecker:
    def __init__(
        self,
        lookup: DnsLookup,
        *,
        cache_ttl_seconds: float = 3600.0,
        temporary_cache_ttl_seconds: float = 60.0,
        max_cache_entries: int = 1024,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.lookup = lookup
        self.cache_ttl_seconds = cache_ttl_seconds
        self.temporary_cache_ttl_seconds = temporary_cache_ttl_seconds
        self.max_cache_entries = max_cache_entries
        self._clock: Callable[[], float] = clock or time.monotonic
        self._cache: OrderedDict[str, tuple[float, MailRoutingResult]] = OrderedDict()

    async def check_email(self, email: str) -> MailRoutingResult:
        return await self.check_domain(email.rsplit("@", maxsplit=1)[-1])

    async def check_domain(self, domain: str) -> MailRoutingResult:
        normalized = domain.strip().rstrip(".").casefold()
        now = self._clock()
        cached = self._cache.get(normalized)
        if cached is not None and cached[0] > now:
            self._cache.move_to_end(normalized)
            return cached[1]
        result = await self._resolve(normalized)
        ttl = (
            self.temporary_cache_ttl_seconds if result.temporary_failure else self.cache_ttl_seconds
        )
        self._cache[normalized] = (now + ttl, result)
        self._cache.move_to_end(normalized)
        while len(self._cache) > self.max_cache_entries:
            self._cache.popitem(last=False)
        return result

    async def _resolve(self, domain: str) -> MailRoutingResult:
        if not domain:
            return MailRoutingResult(domain, MailRoutingStatus.NO_DOMAIN, "empty_domain")
        try:
            exchanges = await self.lookup.query(domain, "MX")
        except DnsNameNotFound:
            return MailRoutingResult(domain, MailRoutingStatus.NO_DOMAIN, "nxdomain")
        except DnsTemporaryError as exc:
            return MailRoutingResult(
                domain, MailRoutingStatus.TEMPORARY_FAILURE, f"mx_lookup:{exc}"[:120]
            )
        except DnsNoAnswer:
            exchanges = []
        usable = [item for item in exchanges if item.strip().rstrip(".")]
        if exchanges and not usable:
            return MailRoutingResult(domain, MailRoutingStatus.NULL_MX, "null_mx")
        if usable:
            return MailRoutingResult(domain, MailRoutingStatus.ROUTABLE, "mx")
        # RFC 5321 section 5.1: without MX records the domain itself is the
        # implicit mail exchanger when it has an address record.
        temporary: str | None = None
        for rdtype in ("A", "AAAA"):
            try:
                if await self.lookup.query(domain, rdtype):
                    return MailRoutingResult(
                        domain, MailRoutingStatus.ROUTABLE, f"implicit_mx:{rdtype}"
                    )
            except DnsNameNotFound:
                return MailRoutingResult(domain, MailRoutingStatus.NO_DOMAIN, "nxdomain")
            except DnsNoAnswer:
                continue
            except DnsTemporaryError as exc:
                temporary = f"{rdtype}_lookup:{exc}"[:120]
        if temporary is not None:
            return MailRoutingResult(domain, MailRoutingStatus.TEMPORARY_FAILURE, temporary)
        return MailRoutingResult(domain, MailRoutingStatus.NO_MAIL_ROUTING, "no_mx_or_address")


_DEFAULT_CHECKERS: dict[tuple[float, float], MailRoutingChecker] = {}


def default_mail_routing_checker(
    *, timeout_seconds: float, cache_ttl_seconds: float
) -> MailRoutingChecker:
    """Return a process-wide checker so its cache is shared by sender batches."""

    key = (timeout_seconds, cache_ttl_seconds)
    checker = _DEFAULT_CHECKERS.get(key)
    if checker is None:
        checker = MailRoutingChecker(
            DnspythonLookup(timeout_seconds=timeout_seconds),
            cache_ttl_seconds=cache_ttl_seconds,
        )
        _DEFAULT_CHECKERS[key] = checker
    return checker


__all__ = [
    "PERMANENT_ROUTING_FAILURES",
    "DnsLookup",
    "DnsNameNotFound",
    "DnsNoAnswer",
    "DnsTemporaryError",
    "DnspythonLookup",
    "MailRoutingChecker",
    "MailRoutingResult",
    "MailRoutingStatus",
    "default_mail_routing_checker",
]
