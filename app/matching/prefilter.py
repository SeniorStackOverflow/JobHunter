from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from app.crawlers.parsing.normalization import normalize_for_fingerprint
from app.matching.schemas import DeterministicFilterResult
from app.models.entities import JobPreference, SourceJob, UserProfile
from app.models.enums import JobStatus, MatchDecision

_MAX_UNTRUSTED_TEXT = 100_000

_PROMPT_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_previous_instructions",
        re.compile(
            r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|system)\s+"
            r"instructions|игнорир\w*\s+(?:все\s+)?(?:предыдущ\w*|системн\w*)\s+инструкц\w*|"
            r"ignor[ăa]\s+(?:toate\s+)?instrucțiunile\s+(?:anterioare|de\s+sistem)",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_exfiltration",
        re.compile(
            r"(?:reveal|show|print|repeat|extract).{0,40}(?:system\s+prompt|hidden\s+instructions)|"
            r"(?:раскрой|покажи|выведи).{0,40}(?:системн\w+\s+промпт|скрыт\w+\s+инструкц)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "credential_exfiltration",
        re.compile(
            r"(?:reveal|send|return|print|upload).{0,50}"
            r"(?:oauth|access\s+token|refresh\s+token|api\s+key|password|secret)|"
            r"(?:раскрой|отправь|покажи).{0,50}(?:oauth|токен|api.?ключ|парол|секрет)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "recipient_or_attachment_override",
        re.compile(
            r"(?:assistant|agent|llm|model).{0,60}(?:change|override|replace).{0,30}"
            r"(?:recipient|email|attachment|resume|cv)|"
            r"(?:attach|upload).{0,30}(?:/etc/|\.\./|all\s+(?:files|documents)|server\s+file)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "policy_override",
        re.compile(
            r"(?:disable|bypass|override|ignore).{0,40}"
            r"(?:policy|safety|daily\s+limit|rate\s+limit|approval)|"
            r"(?:отключи|обойди|игнорируй).{0,40}(?:политик|безопасност|лимит|подтвержден)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "tool_invocation_instruction",
        re.compile(
            r"(?:call|invoke|execute|run).{0,30}(?:mcp\s+tool|shell|terminal|command)|"
            r"(?:вызови|запусти|выполни).{0,30}(?:mcp|команд|терминал|shell)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)

_SCAM_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "upfront_payment",
        re.compile(
            r"(?:pay|fee|deposit|registration).{0,40}(?:before|upfront|to\s+start)|"
            r"(?:оплат|взнос|депозит).{0,40}(?:до\s+начала|заранее|регистрац)|"
            r"(?:tax[ăa]|depunere|depozit).{0,40}(?:înainte|pentru\s+începere)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "crypto_transfer",
        re.compile(
            r"(?:crypto|bitcoin|btc|usdt|wallet).{0,40}(?:payment|transfer|deposit)|"
            r"(?:крипт|биткоин|usdt|кошел[её]к).{0,40}(?:оплат|перевод|депозит)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "sensitive_financial_credentials",
        re.compile(
            r"(?:send|provide|share).{0,50}(?:bank\s+card|pin|password|seed\s+phrase)|"
            r"(?:отправ|предостав|сообщ).{0,50}(?:банковск\w+\s+карт|pin|парол|сид.?фраз)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "money_mule_activity",
        re.compile(
            r"(?:receive|accept).{0,35}(?:money|funds).{0,35}(?:forward|transfer)|"
            r"(?:получать|принимать).{0,35}(?:деньги|средства).{0,35}(?:переводить|отправлять)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)

_NO_EXPERIENCE = re.compile(
    r"\bno\s+experience\b|\bwithout\s+experience\b|без\s+опыта|f[ăa]r[ăa]\s+experien[țt][ăa]",
    re.IGNORECASE,
)
_DRIVING_LICENCE = re.compile(
    r"driver'?s?\s+licen[cs]e|driving\s+licen[cs]e|водительск\w+\s+прав|"
    r"permis\s+de\s+conducere|categoria\s+[abcd](?:\b|\d)",
    re.IGNORECASE,
)
_DRIVING_OPTIONAL = re.compile(
    r"preferred|advantage|nice\s+to\s+have|would\s+be\s+a\s+plus|is\s+a\s+plus|"
    r"binevenit|constitui(?:e|\s+un)\s+avantaj|poate\s+constitui\s+un\s+avantaj|avantaj|"
    r"желательн|приветств|будет\s+преимуществ|не\s+обязател",
    re.IGNORECASE,
)
_DRIVING_REQUIRED = re.compile(
    r"mandatory|required|must\s+have|obligatori|necesar|este\s+necesar|trebuie\s+(?:s[ăa]\s+)?(?:ai|de[țt]ii|posezi)|"
    r"обязател|требуется|необходим",
    re.IGNORECASE,
)


def _driving_licence_is_hard_requirement(text: str) -> bool:
    """Require a licence only when the vacancy actually marks it as mandatory.

    Source-taxonomy category `drivers` is not enough: it also contains bicycle/courier
    jobs, and many postings mention a licence only as an advantage.
    """
    for match in _DRIVING_LICENCE.finditer(text):
        start = max(0, text.rfind("\n", 0, match.start()) + 1)
        end = text.find("\n", match.end())
        if end == -1:
            end = min(len(text), match.end() + 220)
        segment = text[start:end][:500]
        # Explicit optional language wins inside the same requirement/list item.
        if _DRIVING_OPTIONAL.search(segment):
            continue
        if _DRIVING_REQUIRED.search(segment):
            return True

        # Also inspect a small local window for forms such as
        # "Permis de conducere categoria B - obligatoriu".
        local = text[max(0, match.start() - 120) : min(len(text), match.end() + 160)]
        if _DRIVING_OPTIONAL.search(local):
            continue
        if _DRIVING_REQUIRED.search(local):
            return True
    return False


# Rabota.md exposes source-taxonomy slugs while profile preferences use the
# application's stable category vocabulary. Keep this translation deterministic
# and deliberately narrow; free-form vacancy text must not silently widen an
# allowed category.
_SOURCE_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "calls": ("customer_service", "support"),
    "drivers": ("delivery",),
    "it": ("technology",),
    "restaurants": ("hospitality",),
    "tourism": ("hospitality",),
    "warehouses": ("warehouse",),
    "transport": ("logistics",),
}
_LOCATION_CANONICAL: dict[str, str] = {
    "chisinau": "chisinau",
    "chis ina u": "chisinau",
    "кишинев": "chisinau",
    "кишине в": "chisinau",
    "balti": "balti",
    "ba lti": "balti",
    "бельцы": "balti",
}


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _normalize(value: str | None) -> str:
    return normalize_for_fingerprint(value)


def _policy_matches(candidates: Iterable[str], policies: Iterable[str]) -> bool:
    normalized_candidates = [_normalize(item) for item in candidates if _normalize(item)]
    for policy in policies:
        normalized_policy = _normalize(policy)
        if not normalized_policy:
            continue
        policy_tokens = set(normalized_policy.split())
        for candidate in normalized_candidates:
            candidate_tokens = set(candidate.split())
            if normalized_policy == candidate or policy_tokens <= candidate_tokens:
                return True
    return False


def _category_policy_matches(candidates: Iterable[str], policies: Iterable[str]) -> bool:
    expanded: list[str] = []
    for candidate in candidates:
        normalized = _normalize(candidate)
        if not normalized:
            continue
        expanded.append(normalized)
        expanded.extend(_SOURCE_CATEGORY_ALIASES.get(normalized, ()))
    return _policy_matches(expanded, policies)


def _location_policy_matches(candidates: Iterable[str], policies: Iterable[str]) -> bool:
    canonical_candidates = [
        _LOCATION_CANONICAL.get(normalized, normalized)
        for value in candidates
        if (normalized := _normalize(value))
    ]
    canonical_policies = [
        _LOCATION_CANONICAL.get(normalized, normalized)
        for value in policies
        if (normalized := _normalize(value))
    ]
    return _policy_matches(canonical_candidates, canonical_policies)


def _untrusted_job_text(job: SourceJob) -> str:
    fields = (
        job.title,
        job.company,
        job.description,
        job.requirements,
        job.responsibilities,
        job.salary_text,
        job.application_url,
    )
    parts = [item for item in fields if item]
    if job.raw_metadata:
        parts.append(json.dumps(job.raw_metadata, ensure_ascii=False, sort_keys=True, default=str))
    return "\n".join(parts)[:_MAX_UNTRUSTED_TEXT]


def _detect(text: str, rules: tuple[tuple[str, re.Pattern[str]], ...]) -> list[str]:
    return [name for name, pattern in rules if pattern.search(text)]


def _bounded_rule_score(rules: dict[str, Any], key: str, default: int) -> int:
    value = rules.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0, min(100, int(value)))


def _string_list_rule(rules: dict[str, Any], key: str) -> list[str]:
    value = rules.get(key, [])
    if not isinstance(value, list):
        return []
    return _unique(item.strip() for item in value if isinstance(item, str) and item.strip())


def _profile_language_names(profile: UserProfile) -> set[str]:
    result: set[str] = set()
    for value in profile.languages or []:
        if value.get("confirmed") is not True:
            continue
        for key in ("code", "language", "name"):
            item = value.get(key)
            if isinstance(item, str) and _normalize(item):
                result.add(_normalize(item))
    return result


def _required_preference_languages(preference: JobPreference) -> set[str]:
    result: set[str] = set()
    for value in preference.language_constraints or []:
        if value.get("required") is not True:
            continue
        item = next(
            (
                value.get(key)
                for key in ("code", "language", "name")
                if isinstance(value.get(key), str)
            ),
            None,
        )
        if isinstance(item, str) and _normalize(item):
            result.add(_normalize(item))
    return result


class DeterministicPrefilter:
    """Apply trusted user rules before any untrusted text reaches an LLM."""

    def evaluate(
        self,
        job: SourceJob,
        preference: JobPreference,
        profile: UserProfile,
        *,
        resume_fit: int,
    ) -> DeterministicFilterResult:
        if not 0 <= resume_fit <= 100:
            raise ValueError("resume_fit must be between 0 and 100")

        text = _untrusted_job_text(job)
        injection = _detect(text, _PROMPT_INJECTION_RULES)
        scams = _detect(text, _SCAM_RULES)
        if injection or scams:
            safety_reasons = [
                *(f"prompt_injection_detected:{item}" for item in injection),
                *(f"scam_indicator_detected:{item}" for item in scams),
            ]
            return DeterministicFilterResult(
                eligible_for_ai=False,
                resume_fit=resume_fit,
                preference_fit=0,
                overall_fit=0,
                decision=MatchDecision.BLOCK,
                risks=["untrusted_job_content_failed_safety_checks"],
                scam_indicators=scams,
                prompt_injection_indicators=injection,
                reasons=safety_reasons,
            )

        reasons: list[str] = []
        requirements_met: list[str] = []
        missing_requirements: list[str] = []
        risks: list[str] = []
        skip_reasons: list[str] = []
        preference_fit = 100

        def penalize(amount: int, reason: str) -> None:
            nonlocal preference_fit
            preference_fit = max(0, preference_fit - amount)
            risks.append(reason)

        if job.status is not JobStatus.ACTIVE:
            skip_reasons.append(f"job_not_active:{job.status or 'unknown'}")
            preference_fit = 0

        categories = [
            item for item in (job.category, job.subcategory, *(job.categories_seen or [])) if item
        ]
        allowed_categories = preference.allowed_categories or []
        category_allowed = bool(allowed_categories) and _category_policy_matches(
            categories, allowed_categories
        )
        outside_resume_allowed = bool(
            preference.consider_outside_primary_resume and category_allowed
        )

        if _category_policy_matches(categories, preference.forbidden_categories or []):
            skip_reasons.append("category_forbidden")
            preference_fit = 0
        elif allowed_categories and not category_allowed:
            skip_reasons.append("category_not_allowed")
            preference_fit = min(preference_fit, 20)
        elif category_allowed:
            requirements_met.append("category_allowed")

        rules = preference.additional_rules or {}
        forbidden_title_terms = _string_list_rule(rules, "forbidden_title_terms")
        if forbidden_title_terms and _policy_matches([job.title], forbidden_title_terms):
            skip_reasons.append("job_title_forbidden")
            preference_fit = 0

        minimum_resume_fit = _bounded_rule_score(rules, "minimum_resume_fit", 0)
        if resume_fit < minimum_resume_fit:
            if outside_resume_allowed:
                requirements_met.append("outside_resume_category_explicitly_allowed")
                reasons.append("low_resume_fit_allowed_by_outside_resume_preference")
            else:
                skip_reasons.append("resume_fit_below_configured_minimum")

        workplace_type = _normalize(job.workplace_type)
        if workplace_type == "remote" and not preference.remote_allowed:
            skip_reasons.append("remote_work_not_allowed")
            preference_fit = min(preference_fit, 20)
        elif workplace_type == "remote":
            requirements_met.append("remote_work_allowed")

        allowed_cities = preference.allowed_cities or []
        job_locations = [item for item in (*(job.cities or []), job.location) if item]
        if allowed_cities and workplace_type != "remote":
            if not job_locations:
                penalize(20, "job_location_missing")
            elif not _location_policy_matches(job_locations, allowed_cities):
                skip_reasons.append("city_not_allowed")
                preference_fit = min(preference_fit, 20)
            else:
                requirements_met.append("city_allowed")

        schedule = [job.schedule] if job.schedule else []
        if schedule and _policy_matches(schedule, preference.forbidden_schedules or []):
            skip_reasons.append("schedule_forbidden")
            preference_fit = min(preference_fit, 20)
        elif preference.allowed_schedules:
            if not schedule:
                penalize(10, "job_schedule_missing")
            elif not _policy_matches(schedule, preference.allowed_schedules):
                skip_reasons.append("schedule_not_allowed")
                preference_fit = min(preference_fit, 30)
            else:
                requirements_met.append("schedule_allowed")

        if preference.minimum_salary is not None:
            expected_currency = (preference.salary_currency or "").upper()
            job_currency = (job.currency or "").upper()
            if expected_currency and job_currency and expected_currency != job_currency:
                penalize(20, "salary_currency_not_comparable")
            elif not job_currency and expected_currency:
                penalize(15, "salary_currency_missing")
            elif job.salary_max is None:
                penalize(15, "salary_range_missing")
            elif job.salary_max < preference.minimum_salary:
                skip_reasons.append("salary_below_minimum")
                preference_fit = min(preference_fit, 20)
            else:
                requirements_met.append("minimum_salary_met")

        required_experience = job.required_experience or ""
        experience_required = bool(
            required_experience
            and not _NO_EXPERIENCE.search(required_experience)
            and job.no_experience is not True
        )
        if experience_required:
            confirmed_experience = any(
                item.get("confirmed") is True for item in profile.work_experience or []
            )
            if not confirmed_experience:
                missing_requirements.append("confirmed_work_experience")
                skip_reasons.append("required_experience_not_confirmed")
            else:
                requirements_met.append("confirmed_work_experience_present")
                penalize(5, "experience_relevance_requires_review")
        elif job.no_experience is True:
            requirements_met.append("job_allows_no_experience")

        if _driving_licence_is_hard_requirement(text):
            if profile.driving_licences:
                requirements_met.append("driving_licence_confirmed")
            else:
                missing_requirements.append("driving_licence")
                skip_reasons.append("required_driving_licence_not_confirmed")

        required_languages = _required_preference_languages(preference)
        missing_languages = required_languages - _profile_language_names(profile)
        if missing_languages:
            missing_requirements.extend(
                f"confirmed_language:{language}" for language in sorted(missing_languages)
            )
            skip_reasons.append("required_language_not_confirmed")
        elif required_languages:
            requirements_met.append("required_languages_confirmed")

        resume_weight = 0.2 if outside_resume_allowed else 0.45
        overall_fit = round(resume_fit * resume_weight + preference_fit * (1 - resume_weight))
        reasons.extend(skip_reasons)
        if skip_reasons:
            decision = MatchDecision.SKIP
            eligible_for_ai = False
        else:
            decision = MatchDecision.PREPARE_FOR_REVIEW
            eligible_for_ai = True
            reasons.append("deterministic_filters_passed")

        return DeterministicFilterResult(
            eligible_for_ai=eligible_for_ai,
            resume_fit=resume_fit,
            preference_fit=preference_fit,
            overall_fit=overall_fit,
            decision=decision,
            requirements_met=_unique(requirements_met),
            missing_requirements=_unique(missing_requirements),
            risks=_unique(risks),
            reasons=_unique(reasons),
            outside_resume_allowed=outside_resume_allowed,
        )


def deterministic_prefilter(
    job: SourceJob,
    preference: JobPreference,
    profile: UserProfile,
    *,
    resume_fit: int,
) -> DeterministicFilterResult:
    return DeterministicPrefilter().evaluate(
        job,
        preference,
        profile,
        resume_fit=resume_fit,
    )
