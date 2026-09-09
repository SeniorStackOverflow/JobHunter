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
        matching_content_hash="c" * 64,
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


def _ru_profile(**overrides: object) -> UserProfile:
    base: dict[str, object] = {
        "name": "Кандидат",
        "languages": [{"code": "ru", "confirmed": True}],
        "confirmed_facts": [],
    }
    base.update(overrides)
    return UserProfile(**base)  # type: ignore[arg-type]


def test_letter_signature_appends_phone_and_email_when_present() -> None:
    profile = _ru_profile(phone="+373 60 000 000", contact_email="cv@example.com")

    _subject, body, _language, _facts = generate_letter(profile, _job("ru"))

    assert body.endswith(
        "С уважением,\nКандидат\nТел.: +373 60 000 000\nEmail: cv@example.com"  # noqa: RUF001
    )


def test_letter_signature_omits_blank_contact_lines() -> None:
    profile = _ru_profile(phone="   ", contact_email=None)

    _subject, body, _language, _facts = generate_letter(profile, _job("ru"))

    assert body.endswith("С уважением,\nКандидат")  # noqa: RUF001
    assert "Тел." not in body
    assert "Email" not in body


def test_letter_signature_is_localised() -> None:
    ro_profile = _ru_profile(
        languages=[{"code": "ro", "confirmed": True}],
        phone="+373 1",
        contact_email="a@b.co",
    )
    _s, ro_body, ro_lang, _f = generate_letter(ro_profile, _job("ro"))
    assert ro_lang == "ro"
    assert ro_body.endswith("Cu respect,\nКандидат\nTel.: +373 1\nEmail: a@b.co")  # noqa: RUF001

    en_profile = _ru_profile(
        languages=[{"code": "en", "confirmed": True}],
        phone="+373 2",
        contact_email="c@d.co",
    )
    _s2, en_body, en_lang, _f2 = generate_letter(en_profile, _job("en"))
    assert en_lang == "en"
    assert en_body.endswith("Kind regards,\nКандидат\nPhone: +373 2\nEmail: c@d.co")  # noqa: RUF001


def test_letter_body_is_byte_identical_without_contact_fields() -> None:
    profile = _ru_profile()

    _subject, body, _language, _facts = generate_letter(profile, _job("ru"))

    assert body == (
        "Здравствуйте, команда Example!\n\n"
        "Хочу откликнуться на вакансию «Python Developer». "
        "Буду рад обсудить требования и формат работы.\n\n"
        "С уважением,\nКандидат"  # noqa: RUF001
    )


def test_letter_with_contact_lines_is_not_flagged_as_prompt_injection() -> None:
    from app.crawlers.parsing.normalization import detect_prompt_injection

    profile = _ru_profile(phone="+373 60 000 000", contact_email="cv@example.com")
    _subject, body, _language, _facts = generate_letter(profile, _job("ru"))

    assert not detect_prompt_injection(body)
