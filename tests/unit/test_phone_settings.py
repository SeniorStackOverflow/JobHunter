from __future__ import annotations

import pytest

from app.settings.config import Settings


def test_phone_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.phone_agent_enabled is False
    assert settings.phonegate_url == "http://127.0.0.1:8888"
    assert settings.phonegate_auth_token is None
    assert settings.phone_poll_idle_seconds == 1.0
    assert settings.phone_caller_region == "MD"


def test_empty_token_is_unset() -> None:
    settings = Settings(_env_file=None, phonegate_auth_token="   ")
    assert settings.phonegate_auth_token is None


def test_production_requires_token_when_agent_enabled() -> None:
    base = dict(
        _env_file=None,
        environment="production",
        secret_key="x" * 40,
        public_base_url="https://jobs.example.com",
        database_url="postgresql+asyncpg://job_agent:real-pass@db/job_agent",
        admin_password_hash="$argon2id$dummy",
        llm_provider="openai",
        openai_api_key="sk-test",
        openai_model="gpt-x",
        phone_agent_enabled=True,
    )
    with pytest.raises(ValueError, match="PHONEGATE_AUTH_TOKEN"):
        Settings(**base)

    Settings(**base, phonegate_auth_token="a-real-token")  # no raise
