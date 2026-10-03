from __future__ import annotations


class DelucruMdError(Exception):
    """Base error for all Delucru.md adapter operations."""


class DelucruMdAccessDenied(DelucruMdError):
    """The requested URL or action violates Delucru.md access or allowlist policies."""


class DelucruMdParseError(DelucruMdError):
    """Delucru.md page content could not be parsed into expected structures."""


class DelucruMdTemporaryError(DelucruMdError):
    """Delucru.md service or network returned a transient error."""


class DelucruMdDegradedError(DelucruMdError):
    """Delucru.md service returned rate limit, loop, or degraded response."""
