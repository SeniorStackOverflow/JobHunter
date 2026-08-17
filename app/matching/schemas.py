from __future__ import annotations

from typing import Annotated

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

    @model_validator(mode="after")
    def enforce_scam_block(self) -> MatchResult:
        if self.scam_indicators and self.decision is not MatchDecision.BLOCK:
            raise ValueError("scam indicators require the block decision")
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
