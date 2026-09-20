"""Transport factory for the dedicated Rabota.md adapter.

The normal path is A14 SOCKS5 egress. If that whole network identity becomes
unavailable/rejected, ProxyPoolFetcher switches to a validated free HTTP proxy.
Each egress gets an independent WAF token namespace and browser context so AWS
WAF state is never mixed across IP identities.
"""

from __future__ import annotations

import json
from importlib import resources

import httpx
from redis.asyncio import Redis as AsyncRedis

from app.crawlers.adapters.rabota_md.errors import (
    RabotaMdEgressError,
    RabotaMdWafFailClosedError,
)
from app.crawlers.adapters.rabota_md.fallback import FallbackFetcher
from app.crawlers.adapters.rabota_md.fetcher import RabotaMdFetcher
from app.crawlers.adapters.rabota_md.proxy_pool import (
    ProxyEndpoint,
    ProxyPoolFetcher,
    RabotaProxyPool,
)
from app.crawlers.adapters.rabota_md.waf.errors import (
    WafBlocked,
    WafCaptchaRequired,
    WafError,
    WafSolveFailed,
)
from app.crawlers.adapters.rabota_md.waf.http_client import WafHttpClient
from app.crawlers.adapters.rabota_md.waf.solver import AwsWafSolver
from app.crawlers.adapters.rabota_md.waf.token_provider import (
    DEFAULT_TOKEN_KEY,
    EnvTokenBackend,
    MintedWafToken,
    PurePythonSolverBackend,
    StealthBrowserTokenMinterBackend,
    WafTokenBackend,
    WafTokenProvider,
)
from app.crawlers.adapters.rabota_md.waf.watchdog import ScriptWatchdog
from app.crawlers.browser import AWS_WAF_BROWSER_ALLOWED_DOMAINS, StealthPlaywrightBrowser
from app.crawlers.http import AsyncRateLimiter, SecureHttpClient
from app.security.ssrf import Resolver
from app.settings import get_settings

_FALLBACK_WAF_BROWSER_VERSION = "151.0.7922.34"


def _bundled_chromium_version() -> str | None:
    try:
        data = json.loads(
            resources.files("playwright")
            .joinpath("driver/package/browsers.json")
            .read_text(encoding="utf-8")
        )
    except (ModuleNotFoundError, FileNotFoundError, OSError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for browser in data.get("browsers", []):
        if isinstance(browser, dict) and browser.get("name") == "chromium":
            version = browser.get("browserVersion")
            if isinstance(version, str) and version:
                return version
    return None


def default_waf_browser_user_agent() -> str:
    version = _bundled_chromium_version() or _FALLBACK_WAF_BROWSER_VERSION
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version} Safari/537.36"
    )


WAF_SOLVER_USER_AGENT = default_waf_browser_user_agent()


def effective_waf_user_agent(user_agent: str) -> str:
    """Single User-Agent for token minting, browser and crawl requests."""
    cleaned = user_agent.strip()
    if cleaned.startswith("Mozilla/5.0"):
        return cleaned
    if not cleaned:
        return WAF_SOLVER_USER_AGENT
    return f"{WAF_SOLVER_USER_AGENT} {cleaned}"


def _proxy_transport(proxy_url: str | None) -> httpx.AsyncBaseTransport | None:
    if proxy_url is None:
        return None
    return httpx.AsyncHTTPTransport(
        proxy=proxy_url,
        trust_env=False,
        retries=0,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=0),
    )


def _token_keys(namespace: str | None) -> tuple[str, str]:
    if namespace is None:
        return DEFAULT_TOKEN_KEY, f"{DEFAULT_TOKEN_KEY}:refresh_lock"
    token_key = f"{DEFAULT_TOKEN_KEY}:{namespace}"
    return token_key, f"{token_key}:refresh_lock"


def _build_single_waf_fetcher(
    *,
    base_url: str,
    waf_user_agent: str,
    requests_per_minute: int,
    minimum_interval_seconds: float,
    timeout_seconds: float,
    max_redirects: int,
    fallback_transport: str,
    browser_max_navigations_per_page: int,
    resolver: Resolver | None,
    proxy_url: str | None,
    token_namespace: str | None,
) -> RabotaMdFetcher:
    redis = AsyncRedis.from_url(get_settings().redis_url)
    watchdog = ScriptWatchdog(redis)
    limiter = AsyncRateLimiter(
        requests_per_minute, minimum_interval_seconds=minimum_interval_seconds
    )
    solver = AwsWafSolver(
        timeout_seconds=timeout_seconds,
        script_hash_checker=watchdog.allows_script,
        requests_per_minute=requests_per_minute,
        minimum_interval_seconds=minimum_interval_seconds,
        max_redirects=max_redirects,
        resolver=resolver,
        rate_limiter=limiter,
        proxy_url=proxy_url,
    )

    browser: StealthPlaywrightBrowser | None = None
    backends: list[WafTokenBackend] = [
        PurePythonSolverBackend(solver, base_url, waf_user_agent, watchdog),
    ]
    if fallback_transport == "stealth_browser":
        browser = StealthPlaywrightBrowser(
            allowed_domains=AWS_WAF_BROWSER_ALLOWED_DOMAINS,
            requests_per_minute=requests_per_minute,
            minimum_interval_seconds=minimum_interval_seconds,
            timeout_seconds=timeout_seconds,
            proxy_server=proxy_url,
            user_agent=waf_user_agent,
            max_navigations_per_page=browser_max_navigations_per_page,
        )
        backends.append(StealthBrowserTokenMinterBackend(browser, f"{base_url}/ru/vacancies"))
    backends.append(EnvTokenBackend())

    token_key, lock_key = _token_keys(token_namespace)
    provider = WafTokenProvider(redis, backends, token_key=token_key, lock_key=lock_key)
    secure = SecureHttpClient(
        allowed_domains=("rabota.md", "www.rabota.md"),
        user_agent=waf_user_agent,
        requests_per_minute=requests_per_minute,
        minimum_interval_seconds=minimum_interval_seconds,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        resolver=resolver,
        transport=_proxy_transport(proxy_url),
        pin_resolved_addresses=False if proxy_url else None,
        rate_limiter=limiter,
    )
    waf_client = WafHttpClient(secure, provider)
    if browser is None:
        return waf_client
    return FallbackFetcher(waf_client, browser, provider, expected_user_agent=waf_user_agent)


async def _prove_free_waf_candidate(
    *,
    endpoint: ProxyEndpoint,
    base_url: str,
    waf_user_agent: str,
    requests_per_minute: int,
    minimum_interval_seconds: float,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Resolver | None,
) -> None:
    """Prove the solver protocol on one free egress before trusting it.

    The normal solver remains gated by ``ScriptWatchdog``. If the global
    compatibility marker expired while A14 was happily bypassing WAF, a raw
    challenge candidate gets exactly one canary-style proof on its own egress:
    solve with a permissive script-hash checker, then prove the minted token on
    both landing GET and the real AJAX pagination contract. Only that full E2E
    success refreshes the compatibility marker and seeds this egress token.
    """

    settings = get_settings()
    redis = AsyncRedis.from_url(settings.redis_url)
    watchdog = ScriptWatchdog(redis)
    provider: WafTokenProvider | None = None
    client: WafHttpClient | None = None
    proof_ok = False

    try:
        if await watchdog.is_compatible():
            return

        free_timeout = min(timeout_seconds, 15.0)
        limiter = AsyncRateLimiter(
            requests_per_minute,
            minimum_interval_seconds=minimum_interval_seconds,
        )
        solver = AwsWafSolver(
            timeout_seconds=free_timeout,
            script_hash_checker=lambda _digest: True,
            requests_per_minute=requests_per_minute,
            minimum_interval_seconds=minimum_interval_seconds,
            max_redirects=max_redirects,
            resolver=resolver,
            rate_limiter=limiter,
            proxy_url=endpoint.url,
        )
        try:
            token = await solver.solve(base_url, waf_user_agent)
        except (WafCaptchaRequired, WafBlocked) as exc:
            raise RabotaMdWafFailClosedError(
                f"Rabota.md free-proxy protocol proof hit {type(exc).__name__}"
            ) from exc
        except WafError as exc:
            raise RabotaMdEgressError(
                f"Rabota.md free-proxy protocol proof failed ({type(exc).__name__})"
            ) from exc

        script_hash = solver.last_script_hash
        if not script_hash:
            raise RabotaMdEgressError(
                "Rabota.md free-proxy protocol proof produced no script hash"
            )

        class _ProofTokenBackend:
            async def mint(self) -> MintedWafToken:
                raise WafSolveFailed("proof token unexpectedly missing")

        token_key, lock_key = _token_keys(endpoint.token_namespace)
        provider = WafTokenProvider(
            redis,
            [_ProofTokenBackend()],
            token_key=token_key,
            lock_key=lock_key,
        )
        await provider.publish_token(MintedWafToken(token))
        secure = SecureHttpClient(
            allowed_domains=("rabota.md", "www.rabota.md"),
            user_agent=waf_user_agent,
            requests_per_minute=requests_per_minute,
            minimum_interval_seconds=minimum_interval_seconds,
            timeout_seconds=free_timeout,
            max_redirects=max_redirects,
            resolver=resolver,
            transport=_proxy_transport(endpoint.url),
            pin_resolved_addresses=False,
            rate_limiter=limiter,
        )
        client = WafHttpClient(secure, provider)

        landing = await client.get(f"{base_url}/ru/")
        if landing.status_code != 200:
            raise RabotaMdEgressError(
                f"Rabota.md free-proxy proof landing returned HTTP {landing.status_code}"
            )
        referer = f"{base_url}/ru/vacancies/category/others"
        page = await client.post_html_fragment(f"{referer}/2", referer=referer)
        if page.status_code != 200:
            raise RabotaMdEgressError(
                f"Rabota.md free-proxy proof pagination returned HTTP {page.status_code}"
            )

        await watchdog.record_canary_success(script_hash)
        proof_ok = True
    except (WafCaptchaRequired, WafBlocked) as exc:
        raise RabotaMdWafFailClosedError(
            f"Rabota.md free-proxy protocol proof hit {type(exc).__name__}"
        ) from exc
    except WafError as exc:
        raise RabotaMdEgressError(
            f"Rabota.md free-proxy protocol proof failed ({type(exc).__name__})"
        ) from exc
    finally:
        if provider is not None and not proof_ok:
            await provider.invalidate()
        if client is not None:
            await client.aclose()
        else:
            await redis.aclose()


def build_waf_fetcher(
    *,
    base_url: str,
    user_agent: str,
    requests_per_minute: int,
    minimum_interval_seconds: float,
    timeout_seconds: float,
    max_redirects: int,
    fallback_transport: str,
    browser_max_navigations_per_page: int = 50,
    resolver: Resolver | None = None,
) -> RabotaMdFetcher:
    settings = get_settings()
    waf_user_agent = effective_waf_user_agent(user_agent)
    runtime_fallback = settings.rabota_browser_fallback_mode
    if runtime_fallback != "source":
        fallback_transport = runtime_fallback

    if settings.rabota_proxy_pool_enabled:
        primary = (
            settings.rabota_proxy_primary_url.get_secret_value()
            if settings.rabota_proxy_primary_url is not None
            else None
        )
        pool = RabotaProxyPool(
            AsyncRedis.from_url(settings.redis_url),
            primary_url=primary,
            target_url=f"{base_url}/ru/",
            user_agent=waf_user_agent,
            free_fallback_enabled=settings.rabota_proxy_free_fallback_enabled,
            discovery_batch=settings.rabota_proxy_discovery_batch,
            validation_concurrency=settings.rabota_proxy_validation_concurrency,
            validation_timeout_seconds=settings.rabota_proxy_validation_timeout_seconds,
            candidate_ttl_seconds=settings.rabota_proxy_candidate_ttl_seconds,
            ready_ttl_seconds=settings.rabota_proxy_ready_ttl_seconds,
            min_fresh_free=settings.rabota_proxy_min_fresh_free,
            ban_cooldown_seconds=settings.rabota_proxy_ban_cooldown_seconds,
            dead_cooldown_seconds=settings.rabota_proxy_dead_cooldown_seconds,
            primary_dead_cooldown_seconds=settings.rabota_proxy_primary_dead_cooldown_seconds,
        )

        def factory(endpoint: ProxyEndpoint) -> RabotaMdFetcher:
            is_free = endpoint.kind == "free"
            return _build_single_waf_fetcher(
                base_url=base_url,
                waf_user_agent=waf_user_agent,
                requests_per_minute=requests_per_minute,
                minimum_interval_seconds=minimum_interval_seconds,
                timeout_seconds=min(timeout_seconds, 15.0) if is_free else timeout_seconds,
                max_redirects=max_redirects,
                # Public free proxies are accepted only when the pure solver can prove
                # them. Avoid launching Chromium through arbitrary public relays.
                fallback_transport="none" if is_free else fallback_transport,
                browser_max_navigations_per_page=browser_max_navigations_per_page,
                resolver=resolver,
                proxy_url=endpoint.url,
                token_namespace=endpoint.token_namespace,
            )

        async def preflight(endpoint: ProxyEndpoint, fetcher: RabotaMdFetcher) -> None:
            if endpoint.capability == "waf_candidate":
                await _prove_free_waf_candidate(
                    endpoint=endpoint,
                    base_url=base_url,
                    waf_user_agent=waf_user_agent,
                    requests_per_minute=requests_per_minute,
                    minimum_interval_seconds=minimum_interval_seconds,
                    timeout_seconds=timeout_seconds,
                    max_redirects=max_redirects,
                    resolver=resolver,
                )
            landing_url = f"{base_url}/ru/"
            referer = f"{base_url}/ru/vacancies/category/others"
            page_url = f"{referer}/2"
            landing = await fetcher.get(landing_url)
            if landing.status_code != 200:
                raise RabotaMdEgressError(
                    f"Rabota.md free-proxy preflight landing returned HTTP {landing.status_code}"
                )
            page = await fetcher.post_html_fragment(page_url, referer=referer)
            if page.status_code != 200:
                raise RabotaMdEgressError(
                    f"Rabota.md free-proxy preflight pagination returned HTTP {page.status_code}"
                )

        return ProxyPoolFetcher(
            pool,
            factory,
            preflight=preflight,
            max_egress_failovers=settings.rabota_proxy_max_failovers,
            max_preflight_attempts=settings.rabota_proxy_max_preflight_attempts,
        )

    return _build_single_waf_fetcher(
        base_url=base_url,
        waf_user_agent=waf_user_agent,
        requests_per_minute=requests_per_minute,
        minimum_interval_seconds=minimum_interval_seconds,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        fallback_transport=fallback_transport,
        browser_max_navigations_per_page=browser_max_navigations_per_page,
        resolver=resolver,
        proxy_url=None,
        token_namespace=None,
    )
