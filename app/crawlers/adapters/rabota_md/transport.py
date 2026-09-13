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
from app.crawlers.browser import StealthPlaywrightBrowser
from app.crawlers.http import SecureHttpClient
from app.security.ssrf import Resolver
from app.settings import get_settings

# The solver reproduces a real browser session; the crawl itself keeps the
# identifying job-agent UA from the source config.
WAF_SOLVER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


def build_waf_fetcher(
    *,
    base_url: str,
    user_agent: str,
    requests_per_minute: int,
    minimum_interval_seconds: float,
    timeout_seconds: float,
    max_redirects: int,
    fallback_transport: str,
    resolver: Resolver | None = None,
) -> RabotaMdFetcher:
    redis = AsyncRedis.from_url(get_settings().redis_url)
    watchdog = ScriptWatchdog(redis)
    solver = AwsWafSolver(script_hash_checker=watchdog.is_approved)

    browser: StealthPlaywrightBrowser | None = None
    backends: list[WafTokenBackend] = [
        PurePythonSolverBackend(solver, base_url, WAF_SOLVER_USER_AGENT, watchdog),
    ]
    if fallback_transport == "stealth_browser":
        browser = StealthPlaywrightBrowser(
            allowed_domains=(
                "rabota.md",
                "www.rabota.md",
                "token.awswaf.com",
                "captcha.awswaf.com",
            ),
            requests_per_minute=requests_per_minute,
            minimum_interval_seconds=minimum_interval_seconds,
            timeout_seconds=timeout_seconds,
        )
        backends.append(StealthBrowserTokenMinterBackend(browser, f"{base_url}/ru/vacancies"))
    backends.append(EnvTokenBackend())

    provider = WafTokenProvider(redis, backends)
    secure = SecureHttpClient(
        allowed_domains=("rabota.md", "www.rabota.md"),
        user_agent=user_agent,
        requests_per_minute=requests_per_minute,
        minimum_interval_seconds=minimum_interval_seconds,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        resolver=resolver,
    )
    waf_client = WafHttpClient(secure, provider)
    if browser is None:
        return waf_client
    return FallbackFetcher(waf_client, browser, provider)
