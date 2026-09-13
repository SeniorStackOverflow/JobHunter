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
