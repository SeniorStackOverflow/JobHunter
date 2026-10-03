# ruff: noqa: RUF001
from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.crawlers.parsing.normalization import normalize_for_fingerprint

FREE_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "icloud.com",
        "mail.ru",
        "outlook.com",
        "yahoo.com",
        "yandex.ru",
    }
)
SHARED_DOMAINS = FREE_EMAIL_DOMAINS | {
    "delucru.md",
    "rabota.md",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
}


def company_key(value: str | None) -> str:
    normalized = normalize_for_fingerprint(value)
    normalized = re.sub(r"\b(?:s r l|s a|l l c|о о о)\b", "", normalized)
    tokens = normalized.split()
    legal = {"srl", "sa", "llc", "ltd", "inc", "ооо"}
    while tokens and tokens[0] in legal:
        tokens.pop(0)
    while tokens and tokens[-1] in legal:
        tokens.pop()
    return " ".join(tokens)


def company_domain(value: str | None) -> str | None:
    """Accept an explicit company URL, excluding shared services and unsafe URL shapes."""
    if not value:
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").casefold().rstrip(".").removeprefix("www.")
        if (
            parts.scheme not in {"https", "http"}
            or parts.username
            or parts.password
            or parts.port not in {None, 80, 443}
        ):
            return None
    except ValueError:
        return None
    if (
        not host
        or "." not in host
        or any(host == domain or host.endswith(f".{domain}") for domain in SHARED_DOMAINS)
    ):
        return None
    return host
