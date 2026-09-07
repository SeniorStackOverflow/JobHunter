from __future__ import annotations

import pytest

import app.database.runtime_preflight as runtime_preflight
from app.database.runtime_preflight import RuntimePreflightError, validate_build_provenance


def test_build_provenance_accepts_matching_production_image() -> None:
    validate_build_provenance(
        actual_flavor="production",
        expected_flavor="production",
        actual_revision="abc123",
        expected_revision="abc123",
    )


def test_build_provenance_rejects_dev_image_in_production() -> None:
    with pytest.raises(RuntimePreflightError, match="build flavor mismatch"):
        validate_build_provenance(
            actual_flavor="development",
            expected_flavor="production",
            actual_revision="abc123",
            expected_revision="abc123",
        )


def test_build_provenance_rejects_wrong_revision() -> None:
    with pytest.raises(RuntimePreflightError, match="image revision mismatch"):
        validate_build_provenance(
            actual_flavor="production",
            expected_flavor="production",
            actual_revision="old123",
            expected_revision="new456",
        )


@pytest.mark.asyncio
async def test_run_preflight_accepts_expected_database_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_BUILD_FLAVOR", "production")
    monkeypatch.setenv("EXPECTED_BUILD_FLAVOR", "production")
    monkeypatch.setenv("APP_REVISION", "abc123")
    monkeypatch.setenv("EXPECTED_APP_REVISION", "abc123")
    monkeypatch.setenv("EXPECTED_DATABASE_ROLE", "jobhunter_migrator")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused")

    async def fake_current_database_user(database_url: str) -> str:
        assert database_url == "postgresql+asyncpg://unused"
        return "jobhunter_migrator"

    monkeypatch.setattr(runtime_preflight, "current_database_user", fake_current_database_user)
    await runtime_preflight.run_preflight()


@pytest.mark.asyncio
async def test_run_preflight_rejects_wrong_database_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_BUILD_FLAVOR", "production")
    monkeypatch.setenv("EXPECTED_BUILD_FLAVOR", "production")
    monkeypatch.setenv("APP_REVISION", "abc123")
    monkeypatch.setenv("EXPECTED_APP_REVISION", "abc123")
    monkeypatch.setenv("EXPECTED_DATABASE_ROLE", "jobhunter_migrator")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused")

    async def fake_current_database_user(database_url: str) -> str:
        assert database_url == "postgresql+asyncpg://unused"
        return "jobhunter_app"

    monkeypatch.setattr(runtime_preflight, "current_database_user", fake_current_database_user)
    with pytest.raises(RuntimePreflightError, match="database role mismatch"):
        await runtime_preflight.run_preflight()
