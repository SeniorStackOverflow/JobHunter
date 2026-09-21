from __future__ import annotations

import re
from collections.abc import Iterable

from app.crawlers.parsing.normalization import normalize_for_fingerprint, stable_hash
from app.matching.schemas import (
    HardRequirementAssessment,
    HardRequirementKind,
    HardRequirementStatus,
)
from app.models.entities import SourceJob, UserProfile

HARD_REQUIREMENT_RULES_VERSION = "hard-requirements-v1"

_MANDATORY = re.compile(
    r"mandatory|required|must\s+have|obligatori|este\s+obligatoriu|necesar|"
    r"trebuie\s+(?:s[ăa]\s+)?(?:ai|de[țt]ii|posezi)|"
    r"обязател|требуется|необходим",
    re.IGNORECASE,
)
_OPTIONAL = re.compile(
    r"preferred|advantage|nice\s+to\s+have|would\s+be\s+a\s+plus|"
    r"constitui(?:e|\s+un)\s+avantaj|poate\s+constitui\s+un\s+avantaj|"
    r"желательн|приветств|будет\s+преимуществ|не\s+обязател",
    re.IGNORECASE,
)
_FORKLIFT = re.compile(
    r"fork[\s-]?lift|stivuitor|motostivuitor|"
    r"погрузчик|погрузчика|погрузчике|карщик",
    re.IGNORECASE,
)
_CREDENTIAL = re.compile(
    r"licen[cs]e|certificate|certification|permit|authorization|authorisation|"
    r"permis(?:ul|ului)?|certificat(?:ul|ului)?|atestat|autoriza[țt]ie|"
    r"прав(?:а|/удостоверени[ея])?|удостоверени[ея]|сертификат|лицензи|разрешени",  # noqa: RUF001
    re.IGNORECASE,
)
_DRIVING = re.compile(
    r"driver'?s?\s+licen[cs]e|driving\s+licen[cs]e|"
    r"водительск\w*\s+прав|permis\s+de\s+conducere|categoria\s+[abcd](?:\b|\d)",
    re.IGNORECASE,
)
_EXPERIENCE = re.compile(
    r"experience|experien[țt][ăa]|опыт\s+работ|стаж",
    re.IGNORECASE,
)
_NO_EXPERIENCE = re.compile(
    r"\bno\s+experience\b|\bwithout\s+experience\b|"
    r"без\s+опыта|f[ăa]r[ăa]\s+experien[țt][ăa]",
    re.IGNORECASE,
)
_ROLE_STOPWORDS = {
    "and",
    "the",
    "with",
    "for",
    "from",
    "operator",
    "worker",
    "specialist",
    "manager",
    "работы",
    "работа",
    "сотрудник",
    "оператор",
    "водитель",
    "sofer",
    "șofer",
    "lucrator",
    "lucrător",
    "operatorul",
    "pentru",
    "de",
}


def _job_text(job: SourceJob) -> str:
    return "\n".join(
        item
        for item in (
            job.title,
            job.description,
            job.requirements,
            job.responsibilities,
            job.required_experience,
        )
        if item
    )


def _excerpt(text: str, start: int, end: int, radius: int = 180) -> str:
    raw = text[max(0, start - radius) : min(len(text), end + radius)]
    compact = " ".join(raw.split())
    return compact[:500]


def _requirement_clause(text: str, start: int, end: int) -> str:
    """Return the local list item/sentence that owns a requirement mention."""

    boundaries = "\n;•|.!?"
    left = max((text.rfind(char, 0, start) for char in boundaries), default=-1)
    right_values = [
        position
        for char in boundaries
        if (position := text.find(char, end)) != -1
    ]
    right = min(right_values) if right_values else len(text)
    return " ".join(text[left + 1 : right].split())[:500]


def _normalized_tokens(value: str | None) -> set[str]:
    return {
        token
        for token in normalize_for_fingerprint(value).split()
        if len(token) >= 3 and token not in _ROLE_STOPWORDS
    }


def _confirmed_fact_id(fact: dict[str, object]) -> str:
    return str(fact.get("id") or fact.get("statement") or fact.get("text") or "").strip()


def _confirmed_fact_text(fact: dict[str, object]) -> str:
    parts: list[str] = []
    for key in ("statement", "text"):
        value = fact.get(key)
        if isinstance(value, str):
            parts.append(value)
    keywords = fact.get("keywords")
    if isinstance(keywords, list):
        parts.extend(str(item) for item in keywords if isinstance(item, str))
    return " ".join(parts)


def _negative_fact(fact: dict[str, object]) -> bool:
    polarity = str(fact.get("polarity") or "").casefold()
    identifier = _confirmed_fact_id(fact).casefold()
    return polarity in {"absent", "missing", "false", "negative"} or identifier.startswith(
        "no_"
    ) or identifier.endswith("_absent")


def _fact_matches_terms(fact: dict[str, object], terms: set[str]) -> bool:
    if fact.get("confirmed") is not True:
        return False
    tokens = _normalized_tokens(_confirmed_fact_text(fact))
    return bool(terms and (terms <= tokens or len(terms & tokens) >= min(2, len(terms))))


def _forklift_fact_matches(fact: dict[str, object]) -> bool:
    return fact.get("confirmed") is True and bool(_FORKLIFT.search(_confirmed_fact_text(fact)))


def _forklift_credential_evidence(profile: UserProfile) -> tuple[list[str], list[str]]:
    positive: list[str] = []
    negative: list[str] = []
    for index, value in enumerate(profile.driving_licences or []):
        if _FORKLIFT.search(str(value)):
            positive.append(f"profile.driving_licence:{index}")
    for fact in profile.confirmed_facts or []:
        if not _forklift_fact_matches(fact):
            continue
        fact_id = _confirmed_fact_id(fact)
        if not fact_id:
            continue
        marker = f"profile.confirmed_fact:{fact_id}"
        if _negative_fact(fact):
            negative.append(marker)
        elif _CREDENTIAL.search(_confirmed_fact_text(fact)):
            positive.append(marker)
    return positive, negative


def _forklift_experience_evidence(profile: UserProfile) -> tuple[list[str], list[str]]:
    positive: list[str] = []
    negative: list[str] = []
    for index, item in enumerate(profile.work_experience or []):
        if item.get("confirmed") is not True:
            continue
        text = " ".join(
            str(item.get(key) or "") for key in ("role", "title", "details", "company")
        )
        if _FORKLIFT.search(text):
            positive.append(f"profile.work_experience:{index}")
    for fact in profile.confirmed_facts or []:
        if not _forklift_fact_matches(fact):
            continue
        fact_id = _confirmed_fact_id(fact)
        if not fact_id:
            continue
        marker = f"profile.confirmed_fact:{fact_id}"
        normalized = normalize_for_fingerprint(_confirmed_fact_text(fact))
        fact_text = _confirmed_fact_text(fact).casefold()
        if (
            "experien" not in normalized
            and "opyt" not in normalized
            and "опыт" not in fact_text
        ):
            continue
        if _negative_fact(fact):
            negative.append(marker)
        else:
            positive.append(marker)
    return positive, negative


def _driving_licence_evidence(
    profile: UserProfile, required_terms: set[str]
) -> tuple[list[str], list[str]]:
    positive: list[str] = []
    negative: list[str] = []
    for index, value in enumerate(profile.driving_licences or []):
        value_tokens = _normalized_tokens(str(value))
        category_terms = {item for item in required_terms if len(item) <= 3}
        if not category_terms or category_terms & value_tokens:
            positive.append(f"profile.driving_licence:{index}")
    for fact in profile.confirmed_facts or []:
        if fact.get("confirmed") is not True:
            continue
        text = _confirmed_fact_text(fact)
        if not _DRIVING.search(text):
            continue
        fact_id = _confirmed_fact_id(fact)
        if not fact_id:
            continue
        marker = f"profile.confirmed_fact:{fact_id}"
        if _negative_fact(fact):
            negative.append(marker)
        else:
            positive.append(marker)
    if not positive and not negative and not (profile.driving_licences or []):
        # Unlike generic professional certifications, driving_licences is a
        # dedicated trusted profile field. An explicitly empty verified field is
        # evidence that no ordinary driving licence is recorded for the profile.
        negative.append("profile.driving_licences:empty")
    return positive, negative


def _generic_credential_evidence(
    profile: UserProfile, terms: set[str]
) -> tuple[list[str], list[str]]:
    positive: list[str] = []
    negative: list[str] = []
    for fact in profile.confirmed_facts or []:
        if not _fact_matches_terms(fact, terms):
            continue
        fact_id = _confirmed_fact_id(fact)
        if not fact_id:
            continue
        marker = f"profile.confirmed_fact:{fact_id}"
        if _negative_fact(fact):
            negative.append(marker)
        else:
            positive.append(marker)
    for index, value in enumerate(profile.driving_licences or []):
        value_tokens = _normalized_tokens(str(value))
        if terms and len(terms & value_tokens) >= min(2, len(terms)):
            positive.append(f"profile.driving_licence:{index}")
    return positive, negative


def _generic_role_experience_evidence(
    profile: UserProfile, terms: set[str]
) -> tuple[list[str], list[str]]:
    positive: list[str] = []
    negative: list[str] = []
    for index, item in enumerate(profile.work_experience or []):
        if item.get("confirmed") is not True:
            continue
        text = " ".join(
            str(item.get(key) or "") for key in ("role", "title", "details", "company")
        )
        tokens = _normalized_tokens(text)
        overlap = terms & tokens
        if terms and len(overlap) >= max(1, min(2, len(terms))):
            positive.append(f"profile.work_experience:{index}")
    for fact in profile.confirmed_facts or []:
        if not _fact_matches_terms(fact, terms):
            continue
        fact_id = _confirmed_fact_id(fact)
        if not fact_id:
            continue
        marker = f"profile.confirmed_fact:{fact_id}"
        if _negative_fact(fact):
            negative.append(marker)
        else:
            positive.append(marker)
    return positive, negative


def _status(
    positive: Iterable[str], negative: Iterable[str]
) -> tuple[HardRequirementStatus, list[str]]:
    positive_ids = list(dict.fromkeys(positive))
    negative_ids = list(dict.fromkeys(negative))
    if positive_ids:
        return HardRequirementStatus.MET, positive_ids
    if negative_ids:
        return HardRequirementStatus.MISSING, negative_ids
    return HardRequirementStatus.UNKNOWN, []


def _assessment(
    *,
    requirement_id: str,
    kind: HardRequirementKind,
    label: str,
    source_excerpt: str,
    evidence_terms: Iterable[str],
    positive: Iterable[str],
    negative: Iterable[str],
) -> HardRequirementAssessment:
    status, evidence_ids = _status(positive, negative)
    return HardRequirementAssessment(
        requirement_id=requirement_id,
        kind=kind,
        label=label,
        status=status,
        evidence_ids=evidence_ids,
        evidence_terms=list(dict.fromkeys(evidence_terms)),
        source_excerpt=source_excerpt,
    )


def _dedupe(
    requirements: Iterable[HardRequirementAssessment],
) -> list[HardRequirementAssessment]:
    result: dict[str, HardRequirementAssessment] = {}
    for item in requirements:
        existing = result.get(item.requirement_id)
        if existing is None:
            result[item.requirement_id] = item
            continue
        rank = {
            HardRequirementStatus.UNKNOWN: 0,
            HardRequirementStatus.MET: 1,
            HardRequirementStatus.MISSING: 2,
        }
        if rank[item.status] > rank[existing.status]:
            result[item.requirement_id] = item
    return list(result.values())


class HardRequirementEngine:
    """Extract hard requirements from vacancy text and bind them to trusted evidence only."""

    rules_version = HARD_REQUIREMENT_RULES_VERSION

    def evaluate(
        self, job: SourceJob, profile: UserProfile
    ) -> list[HardRequirementAssessment]:
        text = _job_text(job)
        requirements: list[HardRequirementAssessment] = []

        forklift_matches = list(_FORKLIFT.finditer(text))
        forklift_licence_detected = False
        for match in forklift_matches:
            clause = _requirement_clause(text, match.start(), match.end())
            window = _excerpt(text, match.start(), match.end(), 240)
            if _OPTIONAL.search(clause):
                continue
            if _CREDENTIAL.search(clause) and _MANDATORY.search(clause):
                positive, negative = _forklift_credential_evidence(profile)
                requirements.append(
                    _assessment(
                        requirement_id="forklift_operator_certificate",
                        kind=HardRequirementKind.PROFESSIONAL_CREDENTIAL,
                        label="valid forklift operator licence/certificate",
                        source_excerpt=window,
                        evidence_terms=("forklift", "stivuitor", "погрузчик", "certificate"),
                        positive=positive,
                        negative=negative,
                    )
                )
                forklift_licence_detected = True
                break

        experience_required = bool(
            job.required_experience
            and not _NO_EXPERIENCE.search(job.required_experience)
            and job.no_experience is not True
        )
        if forklift_matches and experience_required:
            for match in forklift_matches:
                clause = _requirement_clause(text, match.start(), match.end())
                window = _excerpt(text, match.start(), match.end(), 220)
                if _EXPERIENCE.search(clause) and not _OPTIONAL.search(clause):
                    positive, negative = _forklift_experience_evidence(profile)
                    requirements.append(
                        _assessment(
                            requirement_id="forklift_operator_experience",
                            kind=HardRequirementKind.ROLE_EXPERIENCE,
                            label="forklift operator work experience",
                            source_excerpt=window,
                            evidence_terms=("forklift", "stivuitor", "погрузчик"),
                            positive=positive,
                            negative=negative,
                        )
                    )
                    break

        for match in _DRIVING.finditer(text):
            clause = _requirement_clause(text, match.start(), match.end())
            window = _excerpt(text, match.start(), match.end(), 180)
            if _FORKLIFT.search(clause):
                continue
            if _OPTIONAL.search(clause):
                continue
            if not _MANDATORY.search(clause):
                continue
            terms = _normalized_tokens(clause)
            positive, negative = _driving_licence_evidence(profile, terms)
            requirements.append(
                _assessment(
                    requirement_id="driving_licence",
                    kind=HardRequirementKind.DRIVING_LICENCE,
                    label="mandatory driving licence",
                    source_excerpt=window,
                    evidence_terms=sorted(terms)[:20],
                    positive=positive,
                    negative=negative,
                )
            )
            break

        # Generic mandatory credential fallback. It intentionally produces UNKNOWN
        # rather than guessing that a similar skill/role proves a professional permit.
        for match in _CREDENTIAL.finditer(text):
            clause = _requirement_clause(text, match.start(), match.end())
            window = _excerpt(text, match.start(), match.end(), 160)
            if _OPTIONAL.search(clause) or not _MANDATORY.search(clause):
                continue
            if forklift_licence_detected and _FORKLIFT.search(clause):
                continue
            if _DRIVING.search(clause):
                continue
            terms = _normalized_tokens(clause)
            identifier = stable_hash(sorted(terms))[:12]
            positive, negative = _generic_credential_evidence(profile, terms)
            requirements.append(
                _assessment(
                    requirement_id=f"professional_credential:{identifier}",
                    kind=HardRequirementKind.PROFESSIONAL_CREDENTIAL,
                    label="mandatory professional credential",
                    source_excerpt=window,
                    evidence_terms=sorted(terms)[:20],
                    positive=positive,
                    negative=negative,
                )
            )

        # Generic required experience becomes a hard role-specific requirement
        # only when the vacancy text explicitly ties experience to the advertised role.
        if experience_required and not any(
            item.kind is HardRequirementKind.ROLE_EXPERIENCE for item in requirements
        ):
            title_terms = _normalized_tokens(job.title)
            role_experience_excerpt: str | None = None
            for match in _EXPERIENCE.finditer(text):
                window = _excerpt(text, match.start(), match.end(), 160)
                if title_terms & _normalized_tokens(window):
                    role_experience_excerpt = window
                    break
            if title_terms and role_experience_excerpt:
                positive, negative = _generic_role_experience_evidence(profile, title_terms)
                requirements.append(
                    _assessment(
                        requirement_id="role_specific_experience",
                        kind=HardRequirementKind.ROLE_EXPERIENCE,
                        label="role-specific required work experience",
                        source_excerpt=role_experience_excerpt,
                        evidence_terms=sorted(title_terms)[:20],
                        positive=positive,
                        negative=negative,
                    )
                )

        return _dedupe(requirements)


def hard_requirements_snapshot(
    requirements: Iterable[HardRequirementAssessment],
) -> list[dict[str, object]]:
    return [
        item.model_dump(mode="json")
        for item in sorted(requirements, key=lambda value: value.requirement_id)
    ]


def all_hard_requirements_met(
    requirements: Iterable[HardRequirementAssessment],
) -> bool:
    return all(item.status is HardRequirementStatus.MET for item in requirements)


__all__ = [
    "HARD_REQUIREMENT_RULES_VERSION",
    "HardRequirementEngine",
    "all_hard_requirements_met",
    "hard_requirements_snapshot",
]
