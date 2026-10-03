from __future__ import annotations

from app.crawlers.errors import SourceDegradedError


class DelucruMdError(Exception):
    """Base error for all Delucru.md adapter operations."""


class DelucruMdAccessDenied(DelucruMdError, SourceDegradedError):
    """The requested URL or action violates Delucru.md access or allowlist policies."""


class DelucruMdParseError(DelucruMdError):
    """Delucru.md page content could not be parsed into expected structures."""


class DelucruMdTemporaryError(DelucruMdError):
    """Delucru.md service or network returned a transient error."""


class DelucruMdDegradedError(DelucruMdError, SourceDegradedError):
    """Delucru.md service returned rate limit, loop, or degraded response."""
