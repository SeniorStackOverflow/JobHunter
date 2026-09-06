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
    assert s.phone_listen_silence_timeout_seconds == 20.0
    assert s.phone_call_hard_cap_seconds == 180.0
    assert s.phone_orchestrator_poll_seconds == 0.15


def test_blocklist_is_normalized() -> None:
    s = Settings(_env_file=None, phone_answer_blocklist=["+373 60 111 222", "060999888"])
    assert s.phone_answer_blocklist == ["+37360111222", "+37360999888"]


def test_summary_and_telegram_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.phone_summary_llm_enabled is False
    assert s.telegram_enabled is False
    assert s.phone_summary_llm_prefer == "quality"
    assert s.phone_evidence_retention_days == 30
    assert s.effective_summary_model == ""


def test_sms_polling_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.phone_sms_batch == 150
    assert s.phone_sms_poll_interval_seconds == 60
    assert s.phone_sms_sync_stale_after_seconds == 300
    assert s.phone_sms_correlation_pre_skew_seconds == 300
    assert s.phone_sms_correlation_post_window_hours == 24


def test_summary_model_falls_back_to_openai_model() -> None:
    s = Settings(_env_file=None, openai_model="gpt-x")
    assert s.effective_summary_model == "gpt-x"
    s2 = Settings(_env_file=None, openai_model="gpt-x", phone_summary_llm_model="qwen")
    assert s2.effective_summary_model == "qwen"


def test_phone_verification_defaults_and_model_fallbacks() -> None:
    s = Settings(_env_file=None, openai_model="gpt-x")
    assert s.phone_verification_pipeline_version == "phone-2b-v1"
    assert s.phone_verification_llm_timeout_seconds == 60.0
    assert s.phone_verification_max_attempts == 3
    assert s.phone_verification_asr_floor == 0.70
    assert s.phone_verification_processing_lease_seconds == 300
    assert s.phone_verification_batch == 10
    assert s.effective_phone_verification_extractor_model == "gpt-x"
    assert s.effective_phone_verification_verifier_model == "gpt-x"
    assert s.effective_phone_verification_arbiter_model == "gpt-x"
    assert s.effective_phone_verification_sms_model == "gpt-x"


def test_production_enabled_verification_requires_effective_models() -> None:
    base = _prod_base() | {
        "phone_summary_llm_enabled": True,
        "phone_summary_llm_api_key": "router-key",
        "phone_summary_llm_model": "summary-model",
        "phonegate_auth_token": "phonegate-token",
        "phonegate_url": "https://phonegate.example.com",
    }
    Settings(**base)
    with pytest.raises(ValueError, match="explicit model"):
        Settings(**(base | {"phone_summary_llm_model": "", "openai_model": ""}))


def _production_settings(**overrides: object) -> Settings:
    """Build a production Settings with all required fields."""
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
    )
    base.update(overrides)
    return Settings(**base)


def test_production_requires_telegram_creds_when_enabled() -> None:
    with pytest.raises(ValueError, match="TELEGRAM"):
        _production_settings(telegram_enabled=True)


def test_empty_telegram_token_is_unset() -> None:
    assert Settings(_env_file=None, telegram_bot_token="  ").telegram_bot_token is None
