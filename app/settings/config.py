from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite+aiosqlite:///./job-agent.db"
    redis_url: str = "redis://localhost:6379/0"
    public_base_url: str = "http://localhost:8000"
    secret_key: SecretStr = SecretStr("development-only-change-me-32-chars")
    token_encryption_key: SecretStr | None = None

    admin_username: str = "admin"
    admin_password_hash: SecretStr | None = None
    mcp_api_keys_hashed: list[str] = Field(default_factory=list)

    real_email_delivery_enabled: bool = False
    emergency_email_kill_switch: bool = False
    email_provider: Literal["fake", "gmail"] = "fake"
    gmail_client_id: SecretStr | None = None
    gmail_client_secret: SecretStr | None = None
    google_admin_emails: list[str] = Field(default_factory=list)

    llm_provider: Literal["mock", "openai", "gemini", "llmrouter"] = "mock"
    openai_api_key: SecretStr | None = None
    openai_model: str | None = None
    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    llmrouter_api_key: SecretStr | None = None
    llmrouter_base_url: str = "http://127.0.0.1:4000"
    llmrouter_prefer: Literal["fast", "cheap", "quality", "balanced"] = "quality"
    llmrouter_timeout_seconds: float = Field(default=75.0, ge=61.0, le=300.0)
    matching_batch_size: int = Field(default=8, ge=1, le=100)
    matching_priority_batch_size: int = Field(default=4, ge=1, le=100)
    matching_max_jobs_per_cycle: int = Field(default=100, ge=1, le=1000)
    matching_inter_job_delay_seconds: float = Field(default=8.0, ge=0, le=60)
    matching_provider_failure_retry_seconds: int = Field(default=3600, ge=60, le=86400)

    resume_storage_path: Path = Path("./storage/resumes")
    max_resume_bytes: int = 5 * 1024 * 1024
    crawler_user_agent: str = "job-agent/0.1 (+operator contact configured by deployment)"
    outbound_request_timeout_seconds: float = 20.0
    max_redirects: int = 5
    log_level: str = "INFO"
    enable_live_rabota_smoke_test: bool = False

    session_cookie_name: str = "job_agent_session"
    session_ttl_seconds: int = 8 * 60 * 60
    csrf_ttl_seconds: int = 60 * 60

    @field_validator("max_resume_bytes")
    @classmethod
    def validate_resume_size(cls, value: int) -> int:
        if value < 1024 or value > 25 * 1024 * 1024:
            raise ValueError("MAX_RESUME_BYTES must be between 1 KiB and 25 MiB")
        return value

    @field_validator(
        "admin_password_hash",
        "token_encryption_key",
        "gmail_client_id",
        "gmail_client_secret",
        "openai_api_key",
        "gemini_api_key",
        "llmrouter_api_key",
        mode="before",
    )
    @classmethod
    def empty_secret_is_unset(cls, value: object) -> object | None:
        if value is None:
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        return None if not raw.strip() else value

    @field_validator("google_admin_emails")
    @classmethod
    def normalize_google_admin_emails(cls, value: list[str]) -> list[str]:
        normalized = sorted({item.strip().casefold() for item in value if item.strip()})
        if any("@" not in item or len(item) > 320 for item in normalized):
            raise ValueError("GOOGLE_ADMIN_EMAILS must contain valid email addresses")
        return normalized

    @model_validator(mode="after")
    def validate_secure_production(self) -> "Settings":
        if self.environment != "production":
            return self
        secret = self.secret_key.get_secret_value()
        unsafe_secret_values = {
            "development-only-change-me-32-chars",
            "replace-with-at-least-32-random-characters",
        }
        if len(secret) < 32 or secret in unsafe_secret_values:
            raise ValueError("a unique SECRET_KEY of at least 32 characters is required")
        if not self.public_base_url.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must use HTTPS in production")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("production requires PostgreSQL through the asyncpg driver")
        if "change-me" in self.database_url.casefold():
            raise ValueError("production DATABASE_URL must not contain an example password")
        if self.admin_password_hash is None:
            raise ValueError("ADMIN_PASSWORD_HASH is required in production")
        if self.google_admin_emails and any(
            value is None
            for value in (
                self.token_encryption_key,
                self.gmail_client_id,
                self.gmail_client_secret,
            )
        ):
            raise ValueError("Google admin login requires Gmail credentials and token encryption")
        if self.llm_provider == "mock":
            raise ValueError("LLM_PROVIDER=mock is forbidden in production")
        if not self.openai_model:
            raise ValueError("an explicit model name is required in production")
        if self.llm_provider == "openai" and self.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required for the OpenAI provider")
        if self.llm_provider == "gemini" and self.gemini_api_key is None:
            raise ValueError("GEMINI_API_KEY is required for the Gemini provider")
        if self.llm_provider == "llmrouter" and self.llmrouter_api_key is None:
            raise ValueError("LLMROUTER_API_KEY is required for the llmRouter provider")
        if self.real_email_delivery_enabled:
            required = (
                self.token_encryption_key,
                self.gmail_client_id,
                self.gmail_client_secret,
            )
            if self.email_provider != "gmail" or any(value is None for value in required):
                raise ValueError(
                    "real Gmail delivery requires Gmail credentials and token encryption"
                )
            assert self.token_encryption_key is not None
            if len(self.token_encryption_key.get_secret_value()) < 32:
                raise ValueError("TOKEN_ENCRYPTION_KEY must contain at least 32 characters")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
