from __future__ import annotations

from app.models.entities import JobSource
from app.models.enums import SourceHealth


class SourceControlError(ValueError):
    """A source cannot be moved into the requested operational state safely."""


def enable_source_record(source: JobSource) -> None:
    """Enable crawling while preserving the explicit downstream safety pause."""
    if source.adapter_type.casefold() == "rabota_md":
        configuration = source.configuration or {}
        live_mode = bool(configuration.get("live_mode", True))
        if not live_mode:
            raise SourceControlError(
                "Rabota.md fixture mode cannot be enabled as a persisted source; use the "
                "fixture_source adapter with its exact local transport"
            )
        acknowledged = bool(configuration.get("policy_review_acknowledged", False))
        reference = configuration.get("policy_review_reference")
        if not acknowledged or not isinstance(reference, str) or not reference.strip():
            raise SourceControlError(
                "Rabota.md live mode requires policy_review_acknowledged=true and a "
                "non-empty policy_review_reference"
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
