from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _load(name: str) -> dict[str, object]:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_standard_prod_is_browser_free() -> None:
    compose = _load("docker-compose.prod.yml")
    build = compose["x-production-build"]["build"]
    environment = compose["x-production-app"]["environment"]
    assert build["args"]["INSTALL_PLAYWRIGHT"] == "0"
    assert environment["RABOTA_BROWSER_FALLBACK_MODE"] == "none"


def test_emergency_overlay_only_promotes_crawler_worker() -> None:
    overlay = _load("docker-compose.browser.yml")
    assert set(overlay["services"]) == {"worker"}
    worker = overlay["services"]["worker"]
    assert worker["image"].startswith("jobhunter-prod-browser:")
    assert worker["build"]["args"]["INSTALL_PLAYWRIGHT"] == "1"
    assert worker["environment"]["RABOTA_BROWSER_FALLBACK_MODE"] == "stealth_browser"


def test_browser_build_is_conditional_and_source_default_is_http_only() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG INSTALL_PLAYWRIGHT=0" in dockerfile
    assert 'if [ "$INSTALL_PLAYWRIGHT" = "1" ]' in dockerfile
    source = _load("config/sources/rabota-md.yaml")["source"]
    assert source["transport"] == "waf_http"
    assert source["fallback_transport"] == "none"


def test_emergency_wrapper_adds_browser_overlay() -> None:
    wrapper = (ROOT / "deploy/prod-browser-compose.sh").read_text(encoding="utf-8")
    assert "docker-compose.browser.yml" in wrapper
    assert "JOBHUNTER_IMAGE_TAG" in wrapper
