from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PolicyDecision


class PolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: PolicyDecision
    rules_passed: list[str] = Field(default_factory=list)
    rules_failed: list[str] = Field(default_factory=list)
    policy_version: str
