from __future__ import annotations

import re

import phonenumbers

_TEL_PREFIX = re.compile(r"^tel:", re.IGNORECASE)


def normalize_e164(value: str | None, *, region: str = "MD") -> str | None:
    """Return an E.164 number, or ``None`` when the input is not a valid number.

    Idempotent on already-normalized input. Accepts a leading ``tel:``,
    international ``00`` prefixes, local Moldovan forms, and free formatting.
    """
    if not value:
        return None
    raw = _TEL_PREFIX.sub("", value.strip())
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    parse_region: str | None
    if raw.startswith("+"):
        candidate, parse_region = f"+{digits}", None
    elif digits.startswith("00"):
        candidate, parse_region = f"+{digits[2:]}", None
    else:
        candidate, parse_region = raw, region
    try:
        parsed = phonenumbers.parse(candidate, parse_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def mask_phone(value: str) -> str:
    """Mask a phone number for logs and audit: keep the country code and last 3."""
    if not value:
        return "(withheld)"
    if value.startswith("+") and len(value) > 7:
        head = value[:4]
        return f"{head}••••{value[-3:]}"
    return f"••••{value[-3:]}" if len(value) > 3 else "•••"
