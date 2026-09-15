from __future__ import annotations

import httpx

from app.crawlers.adapters.rabota_md.errors import RabotaMdDegradedError
from app.crawlers.browser import BrowserNavigationError
from app.crawlers.pipeline import _degradation_reason, _safe_iteration_diagnostics


def test_rabota_degraded_error_is_source_degradation() -> None:
    exc = RabotaMdDegradedError("Rabota.md browser fallback encountered CAPTCHA; fail-closed")
    assert _degradation_reason(exc) == "adapter access degraded: RabotaMdDegradedError"


def test_browser_fragment_failure_has_safe_diagnostics() -> None:
    exc = BrowserNavigationError("browser fragment fetch failed: Error")
    diagnostics = _safe_iteration_diagnostics(exc)
    assert diagnostics["iteration_error"] == "BrowserNavigationError"
    assert diagnostics["iteration_reason"] == "browser_fragment_fetch_failed"
    assert diagnostics["iteration_transport"] == "browser_fallback"
    assert diagnostics["iteration_message"] == "browser fragment fetch failed: Error"


def test_http_timeout_has_safe_transport_diagnostics() -> None:
    exc = httpx.ReadTimeout("timed out")
    diagnostics = _safe_iteration_diagnostics(exc)
    assert diagnostics["iteration_error"] == "ReadTimeout"
    assert diagnostics["iteration_transport"] == "http"
    assert diagnostics["iteration_message"] == "timed out"
