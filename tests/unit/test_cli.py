from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import cli
from app.cli import validate_source_config
from app.models.entities import JobPreference, JobSource, UserProfile
from app.models.enums import SourceHealth


def test_validate_source_config_dispatches_generic_and_rabota() -> None:
    root = Path(__file__).parents[2]

    generic = validate_source_config(root / "config/sources/generic-example.yaml")
    rabota = validate_source_config(root / "config/sources/rabota-md.yaml")

    assert generic["adapter"] == "generic_html"
    assert rabota["base_url"] == "https://www.rabota.md"
    assert rabota["policy_review_acknowledged"] is True
    assert rabota["policy_review_reference"] == "operator-approved-2026-08-11"


@pytest.mark.asyncio
async def test_seed_defaults_creates_safe_profile_and_is_idempotent(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "async_session_factory", sqlite_session_factory)

    await cli.seed_defaults(include_fixture=False)
    await cli.seed_defaults(include_fixture=False)

    async with sqlite_session_factory() as session:
        profile = await session.scalar(select(UserProfile))
        preferences = await session.scalar(select(JobPreference))
        source = await session.scalar(
            select(JobSource).where(JobSource.adapter_type == "rabota_md")
        )
        assert profile is not None
        assert profile.name == "Основной профиль"
        assert profile.is_default is True
        assert preferences is not None
        assert preferences.profile_id == profile.id
        assert preferences.global_pause is True
        assert preferences.auto_send_enabled is False
        assert source is not None
        assert source.enabled is False
        assert source.health_status == SourceHealth.PAUSED
        assert source.automatic_actions_paused is True
        assert await session.scalar(select(func.count(UserProfile.id))) == 1
        assert await session.scalar(select(func.count(JobPreference.id))) == 1
        assert await session.scalar(select(func.count(JobSource.id))) == 1
