from __future__ import annotations

# ruff: noqa: RUF001 — Russian characters in parser patterns are intentional.
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.phone.script import SCRIPT_CLOSING, SCRIPT_CLOSING_SMS

CriticalField = Literal[
    "interview_date",
    "interview_time",
    "timezone",
    "format",
    "address",
    "meeting_url",
    "company",
    "vacancy",
]

_MONTHS: dict[str, int] = {
    "январь": 1,
    "января": 1,
    "февраль": 2,
    "февраля": 2,
    "март": 3,
    "марта": 3,
    "апрель": 4,
    "апреля": 4,
    "май": 5,
    "мая": 5,
    "июнь": 6,
    "июня": 6,
    "июль": 7,
    "июля": 7,
    "август": 8,
    "августа": 8,
    "сентябрь": 9,
    "сентября": 9,
    "октябрь": 10,
    "октября": 10,
    "ноябрь": 11,
    "ноября": 11,
    "декабрь": 12,
    "декабря": 12,
}

_HOUR_WORDS: dict[str, int] = {
    "ноль": 0,
    "ноль часов": 0,
    "один": 1,
    "одна": 1,
    "первый": 1,
    "первого": 1,
    "два": 2,
    "две": 2,
    "второй": 2,
    "второго": 2,
    "три": 3,
    "третий": 3,
    "третьего": 3,
    "четыре": 4,
    "четвёртый": 4,
    "четвертый": 4,
    "четвёртого": 4,
    "четвертого": 4,
    "пять": 5,
    "пятый": 5,
    "пятого": 5,
    "шесть": 6,
    "шестой": 6,
    "шестого": 6,
    "семь": 7,
    "седьмой": 7,
    "седьмого": 7,
    "восемь": 8,
    "восьмой": 8,
    "восьмого": 8,
    "девять": 9,
    "девятый": 9,
    "девятого": 9,
    "десять": 10,
    "десятый": 10,
    "десятого": 10,
    "одиннадцать": 11,
    "одиннадцатый": 11,
    "одиннадцатого": 11,
    "двенадцать": 12,
    "двенадцатый": 12,
    "двенадцатого": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
}

_NUMERIC_DATE_RE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})[./-](?P<month>\d{1,2})"
    r"(?:[./-](?P<year>\d{2,4}))?(?!\d)"
)
_TEXT_DATE_RE = re.compile(
    r"(?<!\w)(?P<day>\d{1,2})\s+(?P<month>[а-яё]+)"
    r"(?:\s+(?P<year>\d{4}))?(?!\w)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_ADDRESS_RE = re.compile(
    r"(?:\b(?:ул\.?|улица|проспект|пр-т|бульвар|бул\.?|переулок|пер\.?|"
    r"шоссе|площадь|пл\.?|str\.?|street)\s+[^,;\n]+[,;]?\s*\d+[а-яa-z]?)",
    re.IGNORECASE,
)
_NUMERIC_TIME_RE = re.compile(r"(?<!\d)(?P<hour>\d{1,2}):(?P<minute>\d{2})(?!\d)")
_CONTEXT_TIME_RE = re.compile(r"(?<!\w)(?:в|на)\s+(?P<hour>\d{1,2})(?!\d|\s*:)")
_HOUR_PHRASE_RE = re.compile(
    r"(?<!\w)(?:(?:в|на)\s+)?(?P<number>[а-яё]+(?:\s+[а-яё]+)?)\s+"
    r"час(?:а|ов)?(?:\s+(?P<period>утра|дня|вечера|ночи))?(?!\w)",
    re.IGNORECASE,
)


def _clean(raw: str) -> str:
    return " ".join(raw.strip().split())


def _local_date(reference_at: datetime, timezone: str) -> date | None:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    aware = reference_at
    if aware.tzinfo is None:
        aware = aware.replace(tzinfo=UTC)
    return aware.astimezone(zone).date()


def _parse_date(raw: str, reference_at: datetime, timezone: str) -> str | None:
    text = _clean(raw).lower()
    if not text:
        return None
    if re.search(r"\b(?:или|либо|примерно|около|возможно|наверное)\b", text):
        return None

    local = _local_date(reference_at, timezone)
    if local is None:
        return None

    candidates: list[date] = []
    relative: dict[str, int] = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
    for word, offset in relative.items():
        if re.search(rf"(?<!\w){word}(?!\w)", text):
            candidates.append(local + timedelta(days=offset))

    for match in _NUMERIC_DATE_RE.finditer(text):
        year_text = match.group("year")
        year = int(year_text) if year_text else local.year
        if year < 100:
            year += 2000
        try:
            candidates.append(date(year, int(match.group("month")), int(match.group("day"))))
        except ValueError:
            return None

    for match in _TEXT_DATE_RE.finditer(text):
        month = _MONTHS.get(match.group("month").lower())
        if month is None:
            continue
        year = int(match.group("year")) if match.group("year") else local.year
        try:
            candidates.append(date(year, month, int(match.group("day"))))
        except ValueError:
            return None

    unique = set(candidates)
    if len(unique) != 1:
        return None
    return next(iter(unique)).isoformat()


def _parse_time(raw: str) -> str | None:
    text = _clean(raw).lower()
    if not text or "после обеда" in text or "после обед" in text:
        return None
    if "минут" in text or re.search(r"\b(?:или|либо|и|примерно|около)\b", text):
        return None

    candidates: list[tuple[int, int]] = []
    for match in _NUMERIC_TIME_RE.finditer(text):
        hour, minute = int(match.group("hour")), int(match.group("minute"))
        if hour > 23 or minute > 59:
            return None
        candidates.append((hour, minute))

    for match in _CONTEXT_TIME_RE.finditer(text):
        hour = int(match.group("hour"))
        if hour > 23:
            return None
        candidates.append((hour, 0))

    for match in _HOUR_PHRASE_RE.finditer(text):
        number = _HOUR_WORDS.get(match.group("number").lower())
        if number is None or number > 23:
            continue
        period = match.group("period")
        if period in {"дня", "вечера"} and 1 <= number <= 11:
            number += 12
        elif period == "ночи" and number == 12:
            number = 0
        candidates.append((number, 0))

    unique = set(candidates)
    if len(unique) != 1:
        return None
    hour, minute = next(iter(unique))
    return f"{hour:02d}:{minute:02d}"


def _parse_timezone(raw: str) -> str | None:
    text = _clean(raw)
    if not text:
        return None
    lower = text.lower()
    russian_zones = {
        "по кишинёвскому времени": "Europe/Chisinau",
        "по кишиневскому времени": "Europe/Chisinau",
        "по молдавскому времени": "Europe/Chisinau",
        "по московскому времени": "Europe/Moscow",
    }
    if lower in russian_zones:
        return russian_zones[lower]
    if lower in {"по местному времени", "местное время", "по нашему времени"}:
        return None

    offset = re.fullmatch(r"(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", lower)
    if offset:
        hours = int(offset.group(2))
        minutes = int(offset.group(3) or 0)
        if hours > 14 or minutes > 59:
            return None
        return f"UTC{offset.group(1)}{hours:02d}:{minutes:02d}"

    if lower in {"utc", "gmt", "eet", "eest"}:
        return lower.upper()
    if "/" not in text:
        return None
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return text


def _parse_format(raw: str) -> str | None:
    text = _clean(raw).lower()
    if not text:
        return None
    if re.search(r"\b(?:или|либо|возможно|наверное|примерно)\b", text):
        return None
    matches: set[str] = set()
    if re.search(r"\b(?:офис(?:е|а)?|очно|на месте|лично)\b", text):
        matches.add("onsite")
    if re.search(r"(?:видеосвяз|видео-звон|онлайн|удал[её]н|zoom|зум|google\s+meet)", text):
        matches.add("remote")
    if re.search(r"\b(?:по телефону|телефонное|телефону|звонок)\b", text):
        matches.add("phone")
    return next(iter(matches)) if len(matches) == 1 else None


def _parse_address(raw: str) -> str | None:
    text = _clean(raw)
    if (
        not text
        or re.search(r"\b(?:или|либо|возможно|наверное)\b", text.lower())
        or not _ADDRESS_RE.search(text)
    ):
        return None
    return text


def _parse_url(raw: str) -> str | None:
    text = _clean(raw)
    matches = _URL_RE.findall(text)
    if len(matches) != 1:
        return None
    return matches[0].rstrip(".,;:!?)]}") or None


def normalize_critical_value(
    field: CriticalField,
    raw: str,
    *,
    reference_at: datetime,
    timezone: str = "Europe/Chisinau",
) -> str | None:
    """Normalize one explicitly quoted critical value conservatively.

    This function intentionally does not choose between competing expressions;
    callers must retain every expression and resolve corrections separately.
    """
    if field == "interview_date":
        return _parse_date(raw, reference_at, timezone)
    if field == "interview_time":
        return _parse_time(raw)
    if field == "timezone":
        return _parse_timezone(raw)
    if field == "format":
        return _parse_format(raw)
    if field == "address":
        return _parse_address(raw)
    if field == "meeting_url":
        return _parse_url(raw)
    if field in {"company", "vacancy"}:
        text = _clean(raw)
        if not text or re.search(r"\b(?:или|либо)\b", text.lower()):
            return None
        return text
    return None


def canonical_critical_value(field: CriticalField, value: str | None) -> str | None:
    """Canonicalize already-normalized values for safe cross-source equality."""
    if value is None:
        return None
    text = " ".join(unicodedata.normalize("NFKC", value).split())
    if not text:
        return None
    if field in {"address", "company", "vacancy"}:
        return text.casefold()
    if field == "meeting_url":
        try:
            parsed = urlsplit(text)
        except ValueError:
            return None
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.casefold().rstrip(".")
        try:
            port = parsed.port
        except ValueError:
            return None
        netloc = host
        if port is not None and not (
            (parsed.scheme.casefold() == "http" and port == 80)
            or (parsed.scheme.casefold() == "https" and port == 443)
        ):
            netloc = f"{host}:{port}"
        normalized = SplitResult(
            parsed.scheme.casefold(),
            netloc,
            parsed.path or "/",
            parsed.query,
            parsed.fragment,
        )
        return urlunsplit(normalized)
    if field in {"timezone", "format"}:
        return text.casefold()
    return text


def has_confirmation_critical_markers(texts: Sequence[str]) -> bool:
    """Return whether any transcript text contains a confirmation marker."""
    for raw in texts:
        text = _clean(raw).lower()
        if not text:
            continue
        if _URL_RE.search(text) or _ADDRESS_RE.search(text):
            return True
        if re.search(r"(?<!\w)(?:сегодня|завтра|послезавтра)(?!\w)", text):
            return True
        if _NUMERIC_DATE_RE.search(text) or _TEXT_DATE_RE.search(text):
            return True
        if re.search(r"(?<!\d)(?:в|на)\s+\d{1,2}(?::\d{2})?(?!\d)", text):
            return True
        if _NUMERIC_TIME_RE.search(text) or _HOUR_PHRASE_RE.search(text):
            return True
        if re.search(
            r"(?:офис(?:е|а)?|очно|на месте|лично|видеосвяз|видео-звон|онлайн|"
            r"удал[её]н|zoom|зум|google\s+meet|по телефону|телефонное)",
            text,
        ):
            return True
        if re.search(
            r"\b(?:собеседован\w*|интервью|встреч\w*|назначен\w*|приходите|"
            r"подключ\w*|созвон\w*)\b",
            text,
        ):
            return True
    return False


def closing_for_transcript(texts: Sequence[str]) -> str:
    return SCRIPT_CLOSING_SMS if has_confirmation_critical_markers(texts) else SCRIPT_CLOSING
