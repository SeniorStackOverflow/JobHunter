from __future__ import annotations

from typing import Protocol

import httpx


class RabotaMdFetcher(Protocol):
    """Transport seam of the Rabota.md adapter.

    Extends the generic ``HttpFetcher`` with the same-site AJAX pagination
    contract. Every transport (waf_http, stealth browser, fallback composite)
    implements all three methods.
    """

    async def get(self, url: str) -> httpx.Response: ...

    async def post_html_fragment(
        self, url: str, *, referer: str | None = None
    ) -> httpx.Response: ...

    async def aclose(self) -> None: ...
