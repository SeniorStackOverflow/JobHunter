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


def _prod_base() -> dict[str, object]:
    return dict(
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


def test_production_requires_token_when_agent_enabled() -> None:
    base = _prod_base()
    with pytest.raises(ValueError, match="PHONEGATE_AUTH_TOKEN"):
        Settings(**base)

    # a routable URL + token -> no raise
    Settings(
        **base,
        phonegate_auth_token="a-real-token",
        phonegate_url="https://phonegate.example.com",
    )


def test_production_rejects_loopback_phonegate_url_when_agent_enabled() -> None:
    base = _prod_base()
    with pytest.raises(ValueError, match="PHONEGATE_URL"):
        Settings(**base, phonegate_auth_token="a-real-token")  # default URL is loopback
    with pytest.raises(ValueError, match="PHONEGATE_URL"):
        Settings(
            **base,
            phonegate_auth_token="a-real-token",
            phonegate_url="http://localhost:8888",
        )
    Settings(
        **base,
        phonegate_auth_token="a-real-token",
        phonegate_url="https://pg.example/",
    )  # no raise
    # a schemeless URL is reported as such, not as "loopback"
    with pytest.raises(ValueError, match="absolute http"):
        Settings(
            **base,
            phonegate_auth_token="a-real-token",
            phonegate_url="phonegate.internal:8888",
        )


def test_non_production_allows_loopback_phonegate_url() -> None:
    # the loopback guard is production-only
    Settings(
        _env_file=None,
        environment="development",
        phone_agent_enabled=True,
        phonegate_auth_token="tok",
        phonegate_url="http://127.0.0.1:8888",
    )


def test_phase_2a_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.phone_auto_answer_enabled is False
    assert s.phone_answer_blocklist == []
    assert s.phone_answer_connect_timeout_seconds == 8.0
    assert s.phone_post_connect_wait_seconds == 1.5
    assert s.phone_speak_fence_timeout_seconds == 5.0
    assert s.phone_tx_idle_timeout_seconds == 30.0
    assert s.phone_inter_block_listen_seconds == 0.8
    assert s.phone_listen_silence_timeout_seconds == 4.0
    assert s.phone_call_hard_cap_seconds == 180.0
    assert s.phone_orchestrator_poll_seconds == 0.15


def test_blocklist_is_normalized() -> None:
    s = Settings(_env_file=None, phone_answer_blocklist=["+373 60 111 222", "060999888"])
    assert s.phone_answer_blocklist == ["+37360111222", "+37360999888"]
