from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.crawlers.registry import build_default_registry
from app.models.entities import JobSource
from app.models.enums import SourceHealth


class SourceControlError(ValueError):
    """A source cannot be moved into the requested operational state safely."""

    def __init__(self, message: str, *, reason: str = "source_control_failed") -> None:
        super().__init__(message)
        self.reason = reason


def source_configuration(source: JobSource) -> dict[str, Any]:
    configuration = source.configuration or {}
    nested = configuration.get("source")
    return nested if isinstance(nested, dict) else configuration


@dataclass(frozen=True)
class SourcePolicyState:
    required: bool
    acknowledged: bool
    reference: str
    live_mode: bool

    @property
    def ready(self) -> bool:
        return self.acknowledged and bool(self.reference.strip())


def source_policy_state(source: JobSource) -> SourcePolicyState:
    configuration = source_configuration(source)
    reference = configuration.get("policy_review_reference")
    return SourcePolicyState(
        required=build_default_registry().requires_policy_review(source.adapter_type),
        acknowledged=configuration.get("policy_review_acknowledged") is True,
        reference=reference if isinstance(reference, str) else "",
        live_mode=configuration.get("live_mode", True) is True,
    )


def enable_source_record(source: JobSource) -> None:
    """Enable crawling while preserving the explicit downstream safety pause."""
    if source.adapter_type.casefold() not in build_default_registry().list_available():
        raise SourceControlError(
            "source adapter is unavailable", reason="source_adapter_unavailable"
        )
    policy = source_policy_state(source)
    if policy.required:
        source_title = source.name
        if not policy.live_mode:
            raise SourceControlError(
                f"{source_title} fixture mode cannot be enabled as a persisted source; use the "
                "fixture_source adapter with its exact local transport",
                reason="source_fixture_only",
            )
        if not policy.ready:
            raise SourceControlError(
                f"{source_title} live mode requires policy_review_acknowledged=true and a "
                "non-empty policy_review_reference",
                reason="source_policy_required",
            )

    source.enabled = True
    # Enabling collection must never implicitly enable matching or applications.
    # An operator can resume downstream actions separately after inspecting a scan.
    source.automatic_actions_paused = True
    # A successful scan must establish HEALTHY before policy-gated applications can send.
    source.health_status = SourceHealth.UNKNOWN


def disable_source_record(source: JobSource) -> None:
    """Disable crawling and every downstream automatic action for this source."""
    source.enabled = False
    source.automatic_actions_paused = True
    source.health_status = SourceHealth.DISABLED
