from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.crawlers.parsing.normalization import stable_hash
from app.models.entities import JobPreference, MatchEvaluation, Resume, UserProfile


def profile_fingerprint(profile: UserProfile) -> str:
    """Hash profile inputs that may affect matching, policy, or letter text."""

    return stable_hash(
        {
            "name": profile.name,
            "contact_email": profile.contact_email,
            "phone": profile.phone,
            "location": profile.location,
            "languages": profile.languages,
            "work_experience": profile.work_experience,
            "education": profile.education,
            "skills": profile.skills,
            "driving_licences": profile.driving_licences,
            "confirmed_facts": profile.confirmed_facts,
            "availability": profile.availability,
        }
    )


def preference_fingerprint(preference: JobPreference) -> str:
    """Hash preferences that affect deterministic filtering or model input.

    Runtime delivery controls (pause, daily quota, auto-send category/threshold)
    are intentionally excluded: the policy engine rechecks them transactionally,
    so toggling a pause must not invalidate otherwise current matching work.
    """

    return stable_hash(
        {
            "allowed_categories": preference.allowed_categories,
            "forbidden_categories": preference.forbidden_categories,
            "allowed_cities": preference.allowed_cities,
            "remote_allowed": preference.remote_allowed,
            "minimum_salary": preference.minimum_salary,
            "salary_currency": preference.salary_currency,
            "allowed_schedules": preference.allowed_schedules,
            "forbidden_schedules": preference.forbidden_schedules,
            "willing_without_experience": preference.willing_without_experience,
            "consider_outside_primary_resume": preference.consider_outside_primary_resume,
            "language_constraints": preference.language_constraints,
            "additional_rules": preference.additional_rules,
        }
    )


def confirmed_fact_hashes(profile: UserProfile) -> dict[str, str]:
    """Return stable hashes keyed by the IDs used in generated applications.

    Duplicate identifiers are hashed as a group rather than silently allowing a
    later item to overwrite an earlier one.
    """

    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for fact in profile.confirmed_facts:
        if fact.get("confirmed") is not True:
            continue
        statement = str(fact.get("statement") or fact.get("text") or "").strip()
        identifier = str(fact.get("id") or statement).strip()
        if identifier:
            grouped[identifier].append(stable_hash(_normalized_fact(fact)))
    return {
        identifier: stable_hash(sorted(hashes)) for identifier, hashes in sorted(grouped.items())
    }


def _normalized_fact(fact: dict[str, Any]) -> dict[str, Any]:
    # Stable hashing already sorts mapping keys; copying prevents a mutable ORM
    # JSON value from changing while it is serialized.
    return dict(fact)


def evaluation_inputs_are_current(
    evaluation: MatchEvaluation,
    profile: UserProfile,
    preference: JobPreference,
    resume: Resume | None,
) -> bool:
    current_resume_id = resume.id if resume is not None else None
    current_resume_sha = resume.sha256 if resume is not None else None
    return (
        evaluation.resume_id == current_resume_id
        and evaluation.resume_sha256 == current_resume_sha
        and evaluation.profile_fingerprint == profile_fingerprint(profile)
        and evaluation.preference_fingerprint == preference_fingerprint(preference)
        and evaluation.confirmed_fact_hashes == confirmed_fact_hashes(profile)
    )


def used_confirmed_facts_are_current(
    evaluation: MatchEvaluation,
    profile: UserProfile,
    used_fact_ids: list[str],
) -> bool:
    bound = evaluation.confirmed_fact_hashes
    if bound is None:
        return False
    current = confirmed_fact_hashes(profile)
    return all(
        fact_id in bound and fact_id in current and bound[fact_id] == current[fact_id]
        for fact_id in used_fact_ids
    )


__all__ = [
    "confirmed_fact_hashes",
    "evaluation_inputs_are_current",
    "preference_fingerprint",
    "profile_fingerprint",
    "used_confirmed_facts_are_current",
]
