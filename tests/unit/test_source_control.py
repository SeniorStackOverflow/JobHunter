from __future__ import annotations

import pytest

from app.crawlers.source_control import (
    SourceControlError,
    disable_source_record,
    enable_source_record,
)
from app.models.entities import JobSource
from app.models.enums import SourceHealth


def _source(adapter_type: str = "generic_html") -> JobSource:
    return JobSource(
        name="Example",
        base_url="https://jobs.example.com",
        adapter_type=adapter_type,
        configuration={},
        enabled=True,
        health_status=SourceHealth.HEALTHY,
        automatic_actions_paused=False,
    )


def test_disable_source_blocks_all_automatic_actions() -> None:
    source = _source()

    disable_source_record(source)

    assert source.enabled is False
    assert source.automatic_actions_paused is True
    assert source.health_status == SourceHealth.DISABLED


def test_enable_requires_fresh_health_and_preserves_safe_downstream_pause() -> None:
    source = _source()
    disable_source_record(source)

    enable_source_record(source)

    assert source.enabled is True
    assert source.automatic_actions_paused is True
    assert source.health_status == SourceHealth.UNKNOWN


def test_rabota_live_enable_requires_documented_policy_review() -> None:
    source = _source("rabota_md")
    source.configuration = {"policy_review_acknowledged": False}

    with pytest.raises(SourceControlError, match="policy_review_reference"):
        enable_source_record(source)

    source.configuration = {
        "live_mode": True,
        "policy_review_acknowledged": True,
        "policy_review_reference": "legal-review-2026-08-03",
    }
    enable_source_record(source)
    assert source.enabled is True


def test_rabota_fixture_mode_cannot_be_enabled_as_persisted_source() -> None:
    source = _source("rabota_md")
    source.configuration = {"live_mode": False}

    with pytest.raises(SourceControlError, match="fixture_source"):
        enable_source_record(source)
