# ruff: noqa: RUF001
from __future__ import annotations

import re
from dataclasses import dataclass

from app.crawlers.parsing.normalization import normalize_for_fingerprint
from app.employers.normalization import company_domain, company_key
from app.models.entities import SourceJob
from app.phone.numbers import normalize_e164

_ROLE_ALIASES = {
    "dishwasher": (
        "dishwasher",
        "посудомойщик",
        "посудомойщица",
        "persoana la spalat vase",
        "spalator de vase",
    ),
    "sales_representative": (
        "sales representative",
        "agent vanzari",
        "agent de vanzari",
        "менеджер по продажам",
        "торговый представитель",
    ),
    "warehouse_operator": ("warehouse operator", "operator depozit", "оператор склада"),
    "warehouse_assistant": (
        "warehouse assistant",
        "warehouse worker",
        "lucrator depozit",
        "работник склада",
    ),
    "courier": ("courier", "curier", "курьер"),
    "cashier": ("cashier", "casier", "кассир"),
    "cleaner": ("cleaner", "работник уборки", "уборщик", "уборщица", "personal curatenie"),
    "cook": ("cook", "bucatar", "повар"),
    "waiter": ("waiter", "ospatar", "официант"),
}
_ROLES = {
    normalize_for_fingerprint(alias): key
    for key, aliases in _ROLE_ALIASES.items()
    for alias in aliases
}
_CITIES = {
    "chisinau": "chisinau",
    "кишинев": "chisinau",
    "kishinev": "chisinau",
    "balti": "balti",
    "бельцы": "balti",
}
_SCHEDULES = {
    normalize_for_fingerprint(alias): value
    for alias, value in {
        "полная занятость": "full time",
        "частичная занятость": "part time",
        "in ture": "shifts",
        "сменный график": "shifts",
        "полный рабочий день": "full time",
    }.items()
}
_EXPERIENCE = {"fara experienta": "no experience", "без опыта": "no experience"}


def token_similarity(left: str | None, right: str | None) -> float:
    a = set(normalize_for_fingerprint(left).split())
    b = set(normalize_for_fingerprint(right).split())
    return len(a & b) / len(a | b) if a and b else 0.0


def role_key(title: str | None) -> str:
    title = normalize_for_fingerprint(title)
    return _ROLES.get(title, title)


def cities(job: SourceJob) -> frozenset[str]:
    values = job.cities or ([job.location] if job.location else [])
    return frozenset(
        _CITIES.get(text, text) for value in values if (text := normalize_for_fingerprint(value))
    )


def emails(job: SourceJob) -> frozenset[str]:
    return frozenset(
        value.strip().casefold()
        for value in [job.public_email, *(job.public_emails or [])]
        if value and "@" in value
    )


def phones(job: SourceJob) -> frozenset[str]:
    return frozenset(
        phone
        for value in [job.public_phone, *(job.public_phones or [])]
        if value and (phone := normalize_e164(value, region="MD"))
    )


def domains(job: SourceJob) -> frozenset[str]:
    values = [f"https://{email.rsplit('@', 1)[1]}" for email in emails(job)]
    metadata = job.raw_metadata or {}
    website = metadata.get("company_website")
    if isinstance(website, str):
        values.append(website)
    # A board profile never supplies an employer domain.
    if job.employer_url:
        values.append(job.employer_url)
    source_domain = metadata.get("source_domain")
    if isinstance(source_domain, str) and metadata.get("source_adapter_type") != "company_careers":
        source_domain = source_domain.casefold().removeprefix("www.")
    else:
        source_domain = None
    return frozenset(
        domain for value in values if (domain := company_domain(value)) and domain != source_domain
    )


def _shift(job: SourceJob) -> frozenset[str]:
    text = normalize_for_fingerprint(f"{job.title} {job.schedule or ''} {job.description or ''}")
    patterns = {
        "night": r"night shift|ночн\w* смен\w*|(?:tura|ture) de noapte",
        "day": r"day shift|дневн\w* смен\w*|(?:tura|ture) de zi",
    }
    return frozenset(key for key, pattern in patterns.items() if re.search(pattern, text))


def conditions_conflict(left: SourceJob, right: SourceJob) -> bool:
    if cities(left) and cities(right) and cities(left) != cities(right):
        return True
    if _shift(left) and _shift(right) and _shift(left).isdisjoint(_shift(right)):
        return True
    left_districts = (left.raw_metadata or {}).get("districts", [])
    right_districts = (right.raw_metadata or {}).get("districts", [])
    if left_districts and right_districts and set(left_districts) != set(right_districts):
        return True
    for field in (
        "schedule",
        "employment_type",
        "workplace_type",
        "required_experience",
        "no_experience",
        "currency",
    ):
        a, b = getattr(left, field), getattr(right, field)
        if a is not None and b is not None:
            a = normalize_for_fingerprint(str(a))
            b = normalize_for_fingerprint(str(b))
            aliases = _EXPERIENCE if field == "required_experience" else _SCHEDULES
            if aliases.get(a, a) != aliases.get(b, b):
                return True
    for field in ("salary_min", "salary_max"):
        a, b = getattr(left, field), getattr(right, field)
        if a is not None and b is not None and a != b:
            return True
    return False


@dataclass(frozen=True)
class DuplicateComparison:
    duplicate: bool = False
    needs_review: bool = False
    score: float = 0.0
    reasons: tuple[str, ...] = ()


def compare_jobs(left: SourceJob, right: SourceJob) -> DuplicateComparison:
    same_employer = bool(left.employer_id and left.employer_id == right.employer_id)
    same_company = bool(
        company_key(left.company) and company_key(left.company) == company_key(right.company)
    )
    shared_email = bool(emails(left) & emails(right))
    shared_phone = bool(phones(left) & phones(right))
    shared_domain = bool(domains(left) & domains(right))
    if not same_employer and not (same_company and (shared_email or shared_phone or shared_domain)):
        return DuplicateComparison()
    exact_title = normalize_for_fingerprint(left.title) == normalize_for_fingerprint(right.title)
    same_role = bool(role_key(left.title) and role_key(left.title) == role_key(right.title))
    if conditions_conflict(left, right):
        return DuplicateComparison(reasons=("different_conditions",))
    if not same_role and token_similarity(left.title, right.title) < 0.8:
        # Unknown translated titles require review; recognized different
        # occupations remain separate even if the board repeats boilerplate.
        left_known = role_key(left.title) in _ROLE_ALIASES
        right_known = role_key(right.title) in _ROLE_ALIASES
        cross_script = bool(re.search(r"[а-яё]", left.title.casefold())) != bool(
            re.search(r"[а-яё]", right.title.casefold())
        )
        substantial_copy = (
            len(normalize_for_fingerprint(left.description).split()) >= 20
            and token_similarity(left.description, right.description) >= 0.95
        )
        if not (left_known and right_known) and (cross_script or substantial_copy):
            return DuplicateComparison(
                False, True, 0.5, ("translated_or_changed_title_requires_review",)
            )
        return DuplicateComparison()
    reasons = tuple(
        reason
        for reason, matches in (
            ("canonical_employer", same_employer),
            ("company_alias", same_company),
            ("shared_email", shared_email),
            ("shared_phone", shared_phone),
            ("company_domain", shared_domain),
            ("role_alias", same_role and not exact_title),
        )
        if matches
    )
    description_match = token_similarity(left.description, right.description)
    requirements_match = token_similarity(left.requirements, right.requirements)
    requirements_agree = not (left.requirements and right.requirements) or requirements_match >= 0.9
    responsibilities_agree = (
        not (left.responsibilities and right.responsibilities)
        or token_similarity(left.responsibilities, right.responsibilities) >= 0.9
    )
    if (
        description_match >= (0.8 if exact_title else 0.9)
        and requirements_agree
        and responsibilities_agree
    ):
        return DuplicateComparison(
            True, False, 0.9 + description_match / 10, (*reasons, "matching_content")
        )
    # Same employer/role without enough comparable content is a review candidate,
    # never a blind merge or an unchecked second delivery.
    return DuplicateComparison(
        False, True, 0.5 + description_match / 4, (*reasons, "content_requires_review")
    )
