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


def test_production_phonegate_overlay_uses_file_secret_and_existing_https_relay() -> None:
    compose = _load("docker-compose.phonegate.prod.yml")
    secret = compose["secrets"]["phonegate_auth_token"]
    assert secret["file"].startswith("${PHONEGATE_AUTH_TOKEN_FILE_HOST:-/etc/jobhunter/")

    for name in ("api", "control-worker", "call-agent"):
        service = compose["services"][name]
        environment = service["environment"]
        assert environment["PHONE_AGENT_ENABLED"] == "true"
        assert environment["PHONEGATE_AUTH_TOKEN"] == ""
        assert environment["PHONEGATE_AUTH_TOKEN_FILE"] == ("/run/secrets/phonegate_auth_token")
        assert "https://phonegate." in environment["PHONEGATE_URL"]
        assert "phonegate_auth_token" in service["secrets"]

    assert compose["services"]["call-agent"]["environment"]["PHONE_AUTO_ANSWER_ENABLED"] == "true"
    assert compose["services"]["call-agent"]["environment"]["PHONE_SUMMARY_LLM_ENABLED"] == "true"


def test_production_phonegate_wrapper_includes_overlay() -> None:
    wrapper = (ROOT / "deploy/prod-phone-compose.sh").read_text(encoding="utf-8")
    assert "docker-compose.phonegate.prod.yml" in wrapper
    assert "JOBHUNTER_IMAGE_TAG" in wrapper

    standard_wrapper = (ROOT / "deploy/prod-compose.sh").read_text(encoding="utf-8")
    assert "/etc/jobhunter/phone-agent-enabled" in standard_wrapper
    assert "docker-compose.phonegate.prod.yml" in standard_wrapper


def test_phonegate_activation_never_places_raw_token_in_prod_env() -> None:
    script = (ROOT / "deploy/activate-prod-phonegate.sh").read_text(encoding="utf-8")
    assert "PHONEGATE_AUTH_TOKEN_FILE" in script
    assert "dotenv_values" in script
    assert "PHONEGATE_AUTH_TOKEN_FILE_HOST=$PHONEGATE_SECRET_FILE" in script
    assert "/api/call/speak" not in script
    assert "marker_preexisting" in script
    assert "os.fsync" in script
