from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import nh3

SPACE_RE = re.compile(r"\s+")
NUMBER_RE = re.compile(r"\d[\d\s.,]*")
EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", re.I)
PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"system\s+prompt", re.I),
    re.compile(r"reveal\s+(oauth|token|secret|credentials?)", re.I),
    re.compile(r"send\s+(?:the\s+)?(?:resume|cv|email)\s+to\s+", re.I),
    re.compile(r"disable\s+(?:the\s+)?(?:limit|policy|safety)", re.I),
    re.compile(r"call\s+(?:an?\s+)?mcp\s+tool", re.I),
)
SCAM_PATTERNS = (
    re.compile(r"(?:pay|fee|deposit).{0,30}(?:before|upfront|registration)", re.I),
    re.compile(r"(?:crypto|bitcoin|usdt).{0,30}(?:payment|transfer|wallet)", re.I),
    re.compile(r"send.{0,30}(?:passport|bank card|pin|password)", re.I),
)


def normalize_whitespace(value: str | None) -> str | None:
    if value is None:
        return None
    result = SPACE_RE.sub(" ", html.unescape(value)).strip()
    return result or None


def sanitize_external_html(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = nh3.clean(value, tags=set(), attributes={})
    return normalize_whitespace(cleaned)


def normalize_for_fingerprint(value: str | None) -> str:
    if not value:
        return ""
    ascii_like = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.findall(r"[\w]+", ascii_like, flags=re.UNICODE))


def stable_hash(*values: object) -> str:
    serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source", "fbclid"}
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urlencode(query), ""))


def _decimal_from_number(value: str) -> Decimal | None:
    cleaned = value.replace(" ", "")
    if cleaned.count(",") == 1 and cleaned.count(".") == 0:
        suffix = cleaned.rsplit(",", maxsplit=1)[1]
        cleaned = cleaned.replace(",", ".") if len(suffix) <= 2 else cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_salary(value: str | None) -> tuple[Decimal | None, Decimal | None, str | None]:
    if not value:
        return None, None, None
    normalized = normalize_whitespace(value) or ""
    numbers = [
        number for match in NUMBER_RE.findall(normalized) if (number := _decimal_from_number(match))
    ]
    currency_map = {
        "€": "EUR",
        "eur": "EUR",
        "$": "USD",
        "usd": "USD",
        "lei": "MDL",
        "mdl": "MDL",
        "ron": "RON",
    }
    lower = normalized.casefold()
    currency = next((code for marker, code in currency_map.items() if marker in lower), None)
    if not numbers:
        return None, None, currency
    if len(numbers) == 1:
        return numbers[0], numbers[0], currency
    return min(numbers[0], numbers[1]), max(numbers[0], numbers[1]), currency


def parse_datetime(value: str | None, formats: list[str] | None = None) -> datetime | None:
    if not value:
        return None
    stripped = value.strip()
    normalized = stripped.replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError:
        result = None
    if result is None:
        for fmt in formats or []:
            try:
                result = datetime.strptime(stripped, fmt)
                break
            except ValueError:
                continue
    if result is None:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def extract_first_email(value: str | None) -> str | None:
    match = EMAIL_RE.search(value or "")
    return match.group(1).lower() if match else None


def extract_first_phone(value: str | None) -> str | None:
    match = PHONE_RE.search(value or "")
    return normalize_whitespace(match.group(0)) if match else None


def detect_prompt_injection(value: str | None) -> list[str]:
    return [pattern.pattern for pattern in INJECTION_PATTERNS if pattern.search(value or "")]


def detect_scam_indicators(value: str | None) -> list[str]:
    return [pattern.pattern for pattern in SCAM_PATTERNS if pattern.search(value or "")]


def content_hash(payload: dict[str, Any]) -> str:
    excluded = {"raw_metadata", "last_seen_at", "first_seen_at"}
    return stable_hash({key: value for key, value in payload.items() if key not in excluded})
