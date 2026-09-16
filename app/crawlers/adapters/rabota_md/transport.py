"""Transport factory for the Rabota.md adapter.

Builds the waf_http stack from docs/sources/rabota-md-http.md:
WafHttpClient over SecureHttpClient, WafTokenProvider with the backend chain
pure-Python solver -> stealth-browser minter -> env token, and FallbackFetcher
when the browser fallback is enabled.
"""

from __future__ import annotations

from redis.asyncio import Redis as AsyncRedis

from app.crawlers.adapters.rabota_md.fallback import FallbackFetcher
from app.crawlers.adapters.rabota_md.fetcher import RabotaMdFetcher
from app.crawlers.adapters.rabota_md.waf.http_client import WafHttpClient
from app.crawlers.adapters.rabota_md.waf.solver import AwsWafSolver
from app.crawlers.adapters.rabota_md.waf.token_provider import (
    EnvTokenBackend,
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

# The solver reproduces a real browser session. Since 2026-09-15 AWS WAF on
# rabota.md binds the aws-waf-token to the User-Agent of the minting session:
# requests with any other UA get a bare 403 on paginated paths. Token minting
# and every crawl request must therefore share one browser-shaped UA.
WAF_SOLVER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


def effective_waf_user_agent(user_agent: str) -> str:
    """Single User-Agent for token minting and all waf_http requests.

    A browser-shaped configured UA is used as-is; an identifying non-browser
    UA (e.g. ``job-agent/0.1``) is appended to the browser base string, which
    keeps both the WAF token binding and the identification policy satisfied.
    A purely synthetic UA is rejected by the WAF on paginated paths even with
    a bound token (verified live 2026-09-15).
    """
    cleaned = user_agent.strip()
    if cleaned.startswith("Mozilla/5.0"):
        return cleaned
    if not cleaned:
        return WAF_SOLVER_USER_AGENT
    return f"{WAF_SOLVER_USER_AGENT} {cleaned}"


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
    waf_user_agent = effective_waf_user_agent(user_agent)
    redis = AsyncRedis.from_url(get_settings().redis_url)
    watchdog = ScriptWatchdog(redis)
    limiter = AsyncRateLimiter(
        requests_per_minute, minimum_interval_seconds=minimum_interval_seconds
    )
    solver = AwsWafSolver(
        script_hash_checker=watchdog.allows_script,
        requests_per_minute=requests_per_minute,
        minimum_interval_seconds=minimum_interval_seconds,
        max_redirects=max_redirects,
        resolver=resolver,
        rate_limiter=limiter,
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
            user_agent=waf_user_agent,
            max_navigations_per_page=browser_max_navigations_per_page,
        )
        backends.append(StealthBrowserTokenMinterBackend(browser, f"{base_url}/ru/vacancies"))
    backends.append(EnvTokenBackend())

    provider = WafTokenProvider(redis, backends)
    secure = SecureHttpClient(
        allowed_domains=("rabota.md", "www.rabota.md"),
        user_agent=waf_user_agent,
        requests_per_minute=requests_per_minute,
        minimum_interval_seconds=minimum_interval_seconds,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        resolver=resolver,
        rate_limiter=limiter,
    )
    waf_client = WafHttpClient(secure, provider)
    if browser is None:
        return waf_client
    return FallbackFetcher(waf_client, browser, provider, expected_user_agent=waf_user_agent)
