from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _load(name: str) -> dict[str, object]:
    with (ROOT / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_dev_phone_services_share_persistent_evidence_volume() -> None:
    compose = _load("docker-compose.yml")
    services = compose["services"]

    for name in ("api", "worker", "call-agent"):
        service = services[name]
        assert service["environment"]["PHONE_EVIDENCE_DIR"] == (
            "${PHONE_EVIDENCE_DIR:-/data/phone_evidence}"
        )
        assert any(
            volume.startswith("phone_evidence:/data/phone_evidence")
            for volume in service["volumes"]
        )


def test_production_phone_runtime_propagates_required_phase_2b_settings() -> None:
    compose = _load("docker-compose.prod.yml")
    environment = compose["x-production-app"]["environment"]

    assert environment["PHONE_EVIDENCE_DIR"] == "/data/phone_evidence"
    for name in (
        "PHONEGATE_URL",
        "PHONEGATE_AUTH_TOKEN",
        "PHONE_AGENT_ENABLED",
        "PHONE_AUTO_ANSWER_ENABLED",
        "PHONE_SUMMARY_LLM_ENABLED",
        "PHONE_SUMMARY_LLM_BASE_URL",
        "PHONE_SUMMARY_LLM_API_KEY",
        "PHONE_SUMMARY_LLM_MODEL",
        "PHONE_SUMMARY_LLM_PREFER",
        "LLMROUTER_BASE_URL",
        "LLMROUTER_API_KEY",
        "LLMROUTER_PREFER",
        "OPENAI_MODEL",
    ):
        assert name in environment

    for name in ("api", "control-worker", "call-agent"):
        assert any(
            volume.startswith("phone_evidence:/data/phone_evidence")
            for volume in compose["services"][name]["volumes"]
        )


def test_runtime_image_prepares_writable_phone_evidence_directory() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "install -d -o jobagent -g jobagent -m 0750 /srv/job-agent /data/resumes" in dockerfile
    assert "/data/phone_evidence" in dockerfile
