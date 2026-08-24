from __future__ import annotations

import pytest

from app.matching.source_version import (
    changes_require_rematch,
    compute_source_matching_hash,
)


def _source(**overrides: object) -> dict[str, object]:
    source: dict[str, object] = {
        "title": "Python Developer",
        "company": "Example",
        "description": "Build services.",
        "location": "Chisinau",
        "cities": ["Chisinau"],
        "status": "active",
        "public_email": "jobs@example.test",
        "published_at": "2026-08-01T10:00:00Z",
        "source_updated_at": "2026-08-01T10:00:00Z",
        "raw_metadata": {"listing_updated_hint": "yesterday"},
    }
    source.update(overrides)
    return source


def test_matching_hash_ignores_crawler_metadata() -> None:
    initial = compute_source_matching_hash(_source())
    refreshed = compute_source_matching_hash(
        _source(
            published_at="2026-08-24T10:00:00Z",
            source_updated_at="2026-08-24T10:00:00Z",
            raw_metadata={"listing_updated_hint": "today"},
        )
    )

    assert refreshed == initial
    assert not changes_require_rematch(
        ["content_hash", "published_at", "source_updated_at", "raw_metadata"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", "Build services and administer PostgreSQL."),
        ("location", "Balti"),
        ("cities", ["Balti"]),
        ("salary_text", "30 000 MDL"),
        ("public_email", "other@example.test"),
        ("status", "possibly_closed"),
    ],
)
def test_matching_hash_changes_for_decision_relevant_fields(
    field: str,
    value: object,
) -> None:
    assert compute_source_matching_hash(_source(**{field: value})) != (
        compute_source_matching_hash(_source())
    )
    assert changes_require_rematch([field])


def test_unknown_snapshot_shape_fails_closed() -> None:
    assert changes_require_rematch(None)
    assert changes_require_rematch({"description": "changed"})
