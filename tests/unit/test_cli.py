from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import cli
from app.cli import validate_source_config
from app.crawlers.adapters.delucru_md import DelucruMdConfig
from app.models.entities import JobPreference, JobSource, UserProfile
from app.models.enums import SourceHealth


def test_validate_source_config_dispatches_generic_and_rabota() -> None:
    root = Path(__file__).parents[2]

    generic = validate_source_config(root / "config/sources/generic-example.yaml")
    rabota = validate_source_config(root / "config/sources/rabota-md.yaml")
    delucru = validate_source_config(root / "config/sources/delucru-md.yaml")

    assert generic["adapter"] == "generic_html"
    assert rabota["base_url"] == "https://www.rabota.md"
    assert rabota["policy_review_acknowledged"] is True
    assert rabota["policy_review_reference"] == "operator-approved-2026-08-11"
    assert delucru["base_url"] == "https://www.delucru.md"
    assert delucru["policy_review_acknowledged"] is True
    assert delucru["policy_review_reference"] == "operator-approved-2026-10-03"


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
        delucru = await session.scalar(
            select(JobSource).where(JobSource.adapter_type == "delucru_md")
        )
        assert delucru is not None
        assert delucru.name == "Delucru.md"
        assert delucru.enabled is False
        assert delucru.health_status == SourceHealth.PAUSED
        assert delucru.automatic_actions_paused is True
        config = DelucruMdConfig.model_validate(delucru.configuration)
        assert config.base_url == delucru.base_url
        assert config.live_mode is True
        assert config.policy_review_acknowledged is True
        assert config.policy_review_reference == "operator-approved-2026-10-03"
        assert await session.scalar(select(func.count(JobSource.id))) == 2


@pytest.mark.asyncio
async def test_seed_registers_missing_source_without_changing_existing_sources_or_preferences(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "async_session_factory", sqlite_session_factory)
    await cli.seed_defaults(include_fixture=False)
    async with sqlite_session_factory() as session:
        sources = list((await session.scalars(select(JobSource))).all())
        existing = {item.adapter_type: item for item in sources}
        rabota = existing["rabota_md"]
        await session.delete(existing["delucru_md"])
        rabota.enabled = True
        rabota.automatic_actions_paused = False
        rabota.health_status = SourceHealth.HEALTHY
        rabota.configuration = {"transport": "waf_http", "operator_setting": "preserve"}
        preferences = await session.scalar(select(JobPreference))
        assert preferences is not None
        preferences.global_pause = False
        profile_id = preferences.profile_id
        rabota_id = rabota.id
        await session.commit()

    await cli.seed_defaults(include_fixture=False)
    await cli.seed_defaults(include_fixture=False)

    async with sqlite_session_factory() as session:
        rabota = await session.get(JobSource, rabota_id)
        assert rabota is not None
        assert rabota.enabled is True
        assert rabota.automatic_actions_paused is False
        assert rabota.health_status == SourceHealth.HEALTHY
        assert rabota.configuration == {"transport": "waf_http", "operator_setting": "preserve"}
        preferences = await session.scalar(select(JobPreference))
        assert preferences is not None
        assert preferences.profile_id == profile_id
        assert preferences.global_pause is False
        assert await session.scalar(select(func.count(JobSource.id))) == 2
