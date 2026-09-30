from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PolicyDecision


class PolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: PolicyDecision
    rules_passed: list[str] = Field(default_factory=list)
    rules_failed: list[str] = Field(default_factory=list)
    policy_version: str
    hard_safety_passed: bool = False
    content_ready: bool = False
    employer_slot_available: bool = False
    soft_match_passed: bool = False
    catchup_stage: int | None = None
    minimum_remaining: int = 0
    target_reservation_day: str | None = None
    target_policy_version: str | None = None
