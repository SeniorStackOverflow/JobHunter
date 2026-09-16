from __future__ import annotations


class RabotaMdError(RuntimeError):
    """Base error raised by the Rabota.md adapter."""


class RabotaMdAccessDenied(RabotaMdError):
    """The requested operation is not permitted by the configured access policy."""


class RabotaMdTemporaryError(RabotaMdError):
    """A retryable source or network error."""


class RabotaMdDegradedError(RabotaMdError):
    """The source appears blocked or structurally degraded."""


class RabotaMdEgressError(RabotaMdDegradedError):
    """The selected network egress is unavailable or rejected by Rabota.md.

    ProxyPoolFetcher may recover this by rotating the whole egress identity. If it
    escapes the pool, the source is genuinely degraded and the pipeline must pause
    automatic actions instead of treating it as a harmless transient timeout.
    """

    def __init__(self, message: str, *, access_rejected: bool = False) -> None:
        super().__init__(message)
        self.access_rejected = access_rejected


class RabotaMdWafFailClosedError(RabotaMdDegradedError):
    """Rabota.md explicitly requested CAPTCHA/block; never rotate identity or transport."""


class RabotaMdParseError(RabotaMdError):
    """A public page could not be parsed safely."""
