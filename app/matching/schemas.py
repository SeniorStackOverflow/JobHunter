from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from app.models.enums import MatchDecision

ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
ReasonText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]


class HardRequirementStatus(StrEnum):
    MET = "met"
    UNKNOWN = "unknown"
    MISSING = "missing"


class HardRequirementKind(StrEnum):
    DRIVING_LICENCE = "driving_licence"
    PROFESSIONAL_CREDENTIAL = "professional_credential"
    ROLE_EXPERIENCE = "role_experience"


class HardRequirementAssessment(BaseModel):
    """Deterministic hard requirement bound only to trusted profile evidence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    requirement_id: ShortText
    kind: HardRequirementKind
    label: ShortText
    status: HardRequirementStatus
    evidence_ids: list[ShortText] = Field(default_factory=list)
    evidence_terms: list[ShortText] = Field(default_factory=list)
    source_excerpt: ShortText | None = None


class MatchResult(BaseModel):
    """Strict, provider-independent output accepted from an LLM matcher."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    resume_fit: int = Field(ge=0, le=100, strict=True)
    preference_fit: int = Field(ge=0, le=100, strict=True)
    overall_fit: int = Field(ge=0, le=100, strict=True)
    requirements_met: list[ShortText]
    missing_requirements: list[ShortText]
    risks: list[ShortText]
    scam_indicators: list[ShortText]
    decision: MatchDecision
    reason: ReasonText
    soft_mismatches: list[
        Literal["skills", "preferred_experience", "resume_relevance", "optional_requirement"]
    ] = Field(default_factory=list)
    optional_requirements_missing: list[ShortText] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def enforce_scam_block(self) -> MatchResult:
        if self.scam_indicators and self.decision is not MatchDecision.BLOCK:
            self.decision = MatchDecision.BLOCK
        return self


class DeterministicFilterResult(BaseModel):
    """Decision and scores produced without consulting an LLM."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    eligible_for_ai: bool
    resume_fit: int = Field(ge=0, le=100)
    preference_fit: int = Field(ge=0, le=100)
    overall_fit: int = Field(ge=0, le=100)
    decision: MatchDecision
    requirements_met: list[ShortText] = Field(default_factory=list)
    missing_requirements: list[ShortText] = Field(default_factory=list)
    risks: list[ShortText] = Field(default_factory=list)
    scam_indicators: list[ShortText] = Field(default_factory=list)
    prompt_injection_indicators: list[ShortText] = Field(default_factory=list)
    reasons: list[ShortText] = Field(default_factory=list)
    hard_requirements: list[HardRequirementAssessment] = Field(default_factory=list)
    outside_resume_allowed: bool = False

    @model_validator(mode="after")
    def enforce_fail_closed_state(self) -> DeterministicFilterResult:
        unsafe = bool(self.scam_indicators or self.prompt_injection_indicators)
        if unsafe and (self.eligible_for_ai or self.decision is not MatchDecision.BLOCK):
            raise ValueError("unsafe external content must be blocked before AI evaluation")
        if self.eligible_for_ai and self.decision is not MatchDecision.PREPARE_FOR_REVIEW:
            raise ValueError("AI-eligible jobs must enter matching as prepare_for_review")
        return self

    def to_match_result(self) -> MatchResult:
        indicators = [*self.scam_indicators]
        indicators.extend(
            f"prompt_injection:{indicator}" for indicator in self.prompt_injection_indicators
        )
        reason = "; ".join(self.reasons) or "deterministic prefilter decision"
        return MatchResult(
            resume_fit=self.resume_fit,
            preference_fit=self.preference_fit,
            overall_fit=self.overall_fit,
            requirements_met=self.requirements_met,
            missing_requirements=self.missing_requirements,
            risks=self.risks,
            scam_indicators=indicators,
            decision=self.decision,
            reason=reason,
        )


class MatchRequest(BaseModel):
    """Minimal, JSON-safe data boundary passed to an LLM provider."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    job_title: ShortText
    company: ShortText | None = None
    category: ShortText | None = None
    subcategory: ShortText | None = None
    location: ShortText | None = None
    schedule: ShortText | None = None
    workplace_type: ShortText | None = None
    salary_text: ShortText | None = None
    description: str | None = None
    requirements: str | None = None
    responsibilities: str | None = None
    profile_skills: list[ShortText] = Field(default_factory=list)
    profile_languages: list[ShortText] = Field(default_factory=list)
    profile_work_experience: list[dict[str, JsonValue]] = Field(default_factory=list)
    profile_education: list[dict[str, JsonValue]] = Field(default_factory=list)
    profile_driving_licences: list[ShortText] = Field(default_factory=list)
    confirmed_facts: list[ShortText] = Field(default_factory=list)
    resume_category: ShortText | None = None
    resume_summary: str | None = None
    preference_context: dict[str, JsonValue] = Field(default_factory=dict)
    deterministic_context: DeterministicFilterResult | None = None


# Keywords that some strict structured-output providers reject. They stay
# enforced by the local Pydantic model; outbound they become description hints.
_VALIDATION_ONLY_KEYWORDS = frozenset(
    {
        "default",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "title",
    }
)


def _bounds_hint(node: dict[str, object]) -> str | None:
    hints: list[str] = []
    if "minimum" in node and "maximum" in node:
        hints.append(f"Integer from {node['minimum']} to {node['maximum']}.")
    if "maxItems" in node:
        hints.append(f"At most {node['maxItems']} items.")
    if "maxLength" in node:
        hints.append(f"At most {node['maxLength']} characters.")
    return " ".join(hints) or None


def _portable_node(node: object, defs: dict[str, object]) -> object:
    if isinstance(node, list):
        return [_portable_node(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    reference = node.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        return _portable_node(defs[reference.removeprefix("#/$defs/")], defs)
    result: dict[str, object] = {}
    for key, value in node.items():
        if key in _VALIDATION_ONLY_KEYWORDS or key == "$defs":
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {name: _portable_node(item, defs) for name, item in value.items()}
        else:
            result[key] = _portable_node(value, defs)
    hint = _bounds_hint(node)
    if hint is not None:
        description = result.get("description")
        result["description"] = f"{description} {hint}" if description else hint
    if result.get("type") == "object" or "properties" in result:
        properties = result.get("properties")
        # Strict structured output requires every property to be required.
        result["required"] = list(properties) if isinstance(properties, dict) else []
        result["additionalProperties"] = False
    return result


def strict_outbound_schema(model: type[BaseModel]) -> dict[str, object]:
    """Return a provider-portable strict JSON Schema for ``model``.

    Every property is required (fields with local defaults must still be sent),
    ``additionalProperties`` is false at every object level, ``$ref`` is inlined
    and validation-only keywords are dropped. The local model keeps validating
    Literal values, bounds and lengths; this only shapes what providers see.
    """

    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    portable = _portable_node(schema, defs if isinstance(defs, dict) else {})
    assert isinstance(portable, dict)
    return portable


MATCH_RESULT_OUTBOUND_SCHEMA = strict_outbound_schema(MatchResult)
