from __future__ import annotations

from uuid import uuid4

import pytest

from app.applications.service import ApplicationPreparationError, generate_letter
from app.models.entities import SourceJob, UserProfile


def _job(locale: str = "ru") -> SourceJob:
    return SourceJob(
        title="Python Developer",
        company="Example",
        description="Python services",
        page_locale=locale,
        source_id=uuid4(),
        external_job_id="fixture",
        canonical_url="https://jobs.example.com/fixture",
        localized_urls={},
        categories_seen=[],
        content_hash="a" * 64,
        source_fingerprint="b" * 64,
        raw_metadata={},
    )


def test_letter_uses_only_a_confirmed_language_and_facts() -> None:
    profile = UserProfile(
        name="Candidate",
        languages=[
            {"code": "ru", "confirmed": False},
            {"code": "en", "confirmed": True},
        ],
        confirmed_facts=[
            {"id": "untrusted", "statement": "Invented Python expert", "confirmed": False},
            {
                "id": "python",
                "statement": "Confirmed Python experience",
                "keywords": ["python"],
                "confirmed": True,
            },
        ],
    )

    subject, body, language, used_facts = generate_letter(profile, _job())

    assert language == "en"
    assert subject == "Application for Python Developer"
    assert "Confirmed Python experience" in body
    assert "Invented" not in body
    assert used_facts == ["python"]


def test_letter_requires_at_least_one_confirmed_supported_language() -> None:
    profile = UserProfile(
        name="Candidate",
        languages=[{"code": "ru", "confirmed": False}],
    )

    with pytest.raises(ApplicationPreparationError, match="confirmed language"):
        generate_letter(profile, _job())
