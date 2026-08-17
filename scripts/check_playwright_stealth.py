from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from app.crawlers.browser import StealthPlaywrightBrowser

SANNYSOFT_URL = "https://bot.sannysoft.com/"


async def run_check(screenshot: Path | None) -> dict[str, Any]:
    browser = StealthPlaywrightBrowser(
        allowed_domains=(
            "bot.sannysoft.com",
            "cdnjs.cloudflare.com",
            "cdn.jsdelivr.net",
        ),
        requests_per_minute=10,
        minimum_interval_seconds=1.2,
        timeout_seconds=45,
    )
    try:
        response = await browser.get(SANNYSOFT_URL)
        await browser.wait(5_000)
        diagnostics = await browser.evaluate(
            r"""
            () => {
              const failed = Array.from(document.querySelectorAll('.failed')).map((node) => {
                const row = node.closest('tr');
                return (row ? row.innerText : node.innerText).replace(/\s+/g, ' ').trim();
              });
              return {
                title: document.title,
                webdriver: navigator.webdriver,
                userAgent: navigator.userAgent,
                platform: navigator.platform,
                languages: Array.from(navigator.languages || []),
                plugins: navigator.plugins ? navigator.plugins.length : 0,
                chromeObject: Boolean(window.chrome),
                failedChecks: failed,
              };
            }
            """
        )
        if screenshot is not None:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            await browser.screenshot(str(screenshot))
        values = diagnostics if isinstance(diagnostics, dict) else {}
        failed_checks = values.get("failedChecks")
        return {
            "url": str(response.url),
            "status": response.status_code,
            "stealth_baseline_passed": (
                response.status_code == 200
                and values.get("webdriver") in {False, None}
                and "HeadlessChrome" not in str(values.get("userAgent", ""))
            ),
            "sannysoft_passed": isinstance(failed_checks, list) and not failed_checks,
            "diagnostics": values,
            "screenshot": str(screenshot) if screenshot is not None else None,
        }
    finally:
        await browser.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--screenshot",
        type=Path,
        default=None,
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_check(args.screenshot)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
