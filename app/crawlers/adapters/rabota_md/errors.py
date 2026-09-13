from __future__ import annotations


class RabotaMdError(RuntimeError):
    """Base error raised by the Rabota.md adapter."""


class RabotaMdAccessDenied(RabotaMdError):
    """The requested operation is not permitted by the configured access policy."""


class RabotaMdTemporaryError(RabotaMdError):
    """A retryable source or network error."""


class RabotaMdDegradedError(RabotaMdError):
    """The source appears blocked or structurally degraded."""


class RabotaMdParseError(RabotaMdError):
    """A public page could not be parsed safely."""
