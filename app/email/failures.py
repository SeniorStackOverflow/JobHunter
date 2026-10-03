"""Recognizing delivery failures from provider diagnostics."""

from __future__ import annotations

# The receiving server could not complete STARTTLS. Gmail never falls back to
# plaintext, so no mailbox of that domain can receive mail until it is fixed.
TLS_FAILURE_MARKERS = (
    "tls negotiation",
    "tls handshake",
    "ssl handshake",
    "starttls",
    "tls connection",
)


def mentions_tls_failure(text: str | None) -> bool:
    folded = (text or "").casefold()
    return any(marker in folded for marker in TLS_FAILURE_MARKERS)
