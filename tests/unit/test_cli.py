from __future__ import annotations

from pathlib import Path

from app.cli import validate_source_config


def test_validate_source_config_dispatches_generic_and_rabota() -> None:
    root = Path(__file__).parents[2]

    generic = validate_source_config(root / "config/sources/generic-example.yaml")
    rabota = validate_source_config(root / "config/sources/rabota-md.yaml")

    assert generic["adapter"] == "generic_html"
    assert rabota["base_url"] == "https://www.rabota.md"
    assert rabota["policy_review_acknowledged"] is True
    assert rabota["policy_review_reference"] == "operator-approved-2026-08-11"
