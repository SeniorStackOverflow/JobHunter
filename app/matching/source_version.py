from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import Enum
from typing import Any

from app.crawlers.parsing.normalization import stable_hash

MATCHING_RELEVANT_SOURCE_FIELDS = frozenset(
    {
        "title",
        "company",
        "employer_url",
        "category",
        "subcategory",
        "description",
        "requirements",
        "responsibilities",
        "salary_text",
        "salary_min",
        "salary_max",
        "currency",
        "location",
        "cities",
        "schedule",
        "employment_type",
        "required_experience",
        "no_experience",
        "workplace_type",
        "public_email",
        "public_phone",
        "public_emails",
        "public_phones",
        "application_url",
        "status",
    }
)


def _source_value(source: object | Mapping[str, Any], field: str) -> Any:
    value = source.get(field) if isinstance(source, Mapping) else getattr(source, field, None)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return sorted(value, key=lambda item: str(item))
    if isinstance(value, tuple):
        return sorted(value, key=lambda item: str(item))
    return value


def compute_source_matching_hash(source: object | Mapping[str, Any]) -> str:
    """Hash only source fields that can change matching, safety, or delivery."""

    payload = {
        field: _source_value(source, field) for field in sorted(MATCHING_RELEVANT_SOURCE_FIELDS)
    }
    return stable_hash(payload)


def changes_require_rematch(changed_fields: object) -> bool:
    """Return whether a stored source revision can invalidate an evaluation."""

    if not isinstance(changed_fields, (list, tuple, set, frozenset)):
        return True
    return any(str(field) in MATCHING_RELEVANT_SOURCE_FIELDS for field in changed_fields)
