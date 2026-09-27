from __future__ import annotations


class WafError(RuntimeError):
    """Base class for AWS WAF transport errors."""


class WafChallengeRequired(WafError):
    """The site answered 202 challenge: the current token is missing or stale."""


class WafUnsupportedChallenge(WafError):
    """The challenge type or protocol shape is not supported by the pure-Python solver."""


class WafScriptVersionUnknown(WafError):
    """Pure solver is gated because no fresh compatibility canary authorizes it."""


class WafPowTimeout(WafError):
    """Proof-of-work exceeded the configured time budget."""


class WafCaptchaRequired(WafError):
    """The site requested a CAPTCHA. Fail-closed: solving CAPTCHA is out of policy."""


class WafBlocked(WafError):
    """The site hard-blocked the request. Fail-closed."""


class WafRateLimited(WafError):
    """The site answered 429. Only Retry-After/backoff is allowed, never fallback."""


class WafPostContractError(WafError):
    """The AJAX pagination POST contract broke (403 with canonical headers)."""


class WafSolveFailed(WafError):
    """The token endpoint rejected the solution or returned no token."""


class WafTransportError(WafError):
    """A network failure occurred at a named stage of the WAF protocol."""

    def __init__(self, *, stage: str, error_type: str) -> None:
        self.stage = stage
        self.error_type = error_type
        super().__init__(f"{error_type} during WAF {stage}")


class WafProofRejected(WafSolveFailed):
    """The WAF proof protocol responded but rejected or omitted a usable token."""

    def __init__(self, *, stage: str, reason_code: str, detail: str) -> None:
        self.stage = stage
        self.reason_code = reason_code
        super().__init__(detail)


class WafBackendExhausted(WafSolveFailed):
    """All configured token backends failed, preserving the primary failure taxonomy."""

    def __init__(
        self,
        failures: tuple[tuple[str, str, str, str], ...],
    ) -> None:
        self.failures = failures
        primary = failures[0] if failures else ("unknown", "UnknownError", "backend", "unknown")
        self.primary_backend = primary[0]
        self.primary_error_type = primary[1]
        self.primary_reason_class = primary[2]
        self.primary_stage = primary[3]
        summary = ", ".join(
            f"{backend}:{error_type}:{reason_class}:{stage}"
            for backend, error_type, reason_class, stage in failures
        )
        super().__init__(f"all WAF token backends failed: {summary}")
