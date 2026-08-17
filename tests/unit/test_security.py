from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin.routes import _safe_external_link
from app.security.crypto import SecretBox, TokenDecryptionError
from app.security.files import UnsafeResumeError, safe_storage_path, validate_resume_upload
from app.security.ssrf import UnsafeURLError, validate_outbound_url
from app.settings import Settings


async def resolver_for(
    address: str,
) -> Callable[[str, int], Awaitable[tuple[str, ...]]]:
    async def resolve(_hostname: str, _port: int) -> tuple[str, ...]:
        return (address,)

    return resolve


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,address",
    [
        ("http://jobs.example.com/x", "127.0.0.1"),
        ("http://jobs.example.com/x", "10.0.0.4"),
        ("http://jobs.example.com/x", "169.254.169.254"),
        ("http://jobs.example.com/x", "::1"),
        ("http://jobs.example.com/x", "fc00::1"),
        ("http://jobs.example.com/x", "fe80::1"),
    ],
)
async def test_ssrf_rejects_non_public_destinations(url: str, address: str) -> None:
    with pytest.raises(UnsafeURLError):
        await validate_outbound_url(url, ["jobs.example.com"], resolver=await resolver_for(address))


@pytest.mark.asyncio
async def test_ssrf_rejects_credentials_and_non_allowlisted_redirect_target() -> None:
    resolver = await resolver_for("93.184.216.34")
    with pytest.raises(UnsafeURLError):
        await validate_outbound_url(
            "https://user:password@jobs.example.com/x",
            ["jobs.example.com"],
            resolver=resolver,
        )
    with pytest.raises(UnsafeURLError):
        await validate_outbound_url(
            "https://attacker.example.net/x", ["jobs.example.com"], resolver=resolver
        )


@pytest.mark.asyncio
async def test_ssrf_accepts_public_allowlisted_subdomain() -> None:
    validated = await validate_outbound_url(
        "https://careers.jobs.example.com/opening#fragment",
        ["jobs.example.com"],
        resolver=await resolver_for("93.184.216.34"),
    )
    assert validated.hostname == "careers.jobs.example.com"
    assert validated.url == "https://careers.jobs.example.com/opening"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/admin",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://user:password@jobs.example.com/opening",
        "https://jobs.example.com:8443/opening",
    ],
)
def test_admin_external_links_reject_internal_or_credentialed_targets(url: str) -> None:
    assert _safe_external_link(url) is None


def test_admin_external_links_allow_public_http_targets() -> None:
    url = "https://jobs.example.com/opening"
    assert _safe_external_link(url) == url


def test_resume_validation_and_path_traversal(tmp_path: Path) -> None:
    valid = validate_resume_upload(
        "../My Résumé.pdf", "application/pdf", b"%PDF-1.7\nfixture", 1024
    )
    assert valid.safe_filename.endswith("My_R_sum_.pdf")
    assert len(valid.sha256) == 64
    with pytest.raises(UnsafeResumeError):
        validate_resume_upload("resume.txt", "text/plain", b"hello", 1024)
    with pytest.raises(UnsafeResumeError):
        safe_storage_path(tmp_path, "../secret.pdf")


def test_encrypted_token_is_authenticated() -> None:
    box = SecretBox("a sufficiently long deployment encryption key")
    encrypted = box.encrypt("refresh-token-value")
    assert box.decrypt(encrypted) == "refresh-token-value"
    with pytest.raises(TokenDecryptionError):
        box.decrypt(encrypted[:-2] + b"aa")


def test_production_settings_reject_development_secret_and_non_https_origin() -> None:
    with pytest.raises(ValueError, match="unique SECRET_KEY"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+asyncpg://user:password@db/jobs",
            public_base_url="https://jobs.example.test",
            admin_password_hash="valid-looking-hash",
        )


def test_production_rejects_mock_matching_and_blank_gmail_encryption() -> None:
    base = {
        "environment": "production",
        "database_url": "postgresql+asyncpg://user:password@db/jobs",
        "public_base_url": "https://jobs.example.test",
        "secret_key": "a-production-only-secret-key-that-is-long-enough",
        "admin_password_hash": "valid-looking-hash",
    }
    with pytest.raises(ValueError, match="mock is forbidden"):
        Settings(_env_file=None, **base)

    with pytest.raises(ValueError, match="Gmail credentials"):
        Settings(
            _env_file=None,
            **base,
            llm_provider="openai",
            openai_model="explicit-model",
            openai_api_key="test-only-key",
            email_provider="gmail",
            real_email_delivery_enabled=True,
            token_encryption_key="",
            gmail_client_id="client",
            gmail_client_secret="secret",
        )

    with pytest.raises(ValueError, match="at least 32"):
        Settings(
            _env_file=None,
            **base,
            llm_provider="openai",
            openai_model="explicit-model",
            openai_api_key="test-only-key",
            email_provider="gmail",
            real_email_delivery_enabled=True,
            token_encryption_key="too-short",
            gmail_client_id="client",
            gmail_client_secret="secret",
        )

    with pytest.raises(ValueError, match="unique SECRET_KEY"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+asyncpg://user:password@db/jobs",
            public_base_url="https://jobs.example.test",
            secret_key="replace-with-at-least-32-random-characters",
            admin_password_hash="valid-looking-hash",
        )

    with pytest.raises(ValueError, match="example password"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+asyncpg://job_agent:change-me@db/jobs",
            public_base_url="https://jobs.example.test",
            secret_key="a-production-only-secret-key-that-is-long-enough",
            admin_password_hash="valid-looking-hash",
        )
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+asyncpg://user:password@db/jobs",
            public_base_url="http://jobs.example.test",
            secret_key="a-production-only-secret-key-that-is-long-enough",
            admin_password_hash="valid-looking-hash",
        )


@pytest.mark.asyncio
async def test_fake_email_provider_is_restricted_to_test_environment(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.email.service import EmailSendBlocked, EmailService

    service = EmailService(
        Settings(environment="development", email_provider="fake"),
        sqlite_session_factory,
    )
    async with sqlite_session_factory() as session:
        with pytest.raises(EmailSendBlocked, match="test environment"):
            await service._provider_for(session)
