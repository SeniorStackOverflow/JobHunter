from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.crawlers.adapters.fixture_source import FixtureSourceAdapter
from app.crawlers.catalog import SourceDefinition, reconcile_source_catalog
from app.crawlers.registry import (
    AdapterRegistryError,
    JobSourceAdapterRegistry,
    build_default_registry,
)
from app.models.entities import JobSource, UserProfile
from app.models.enums import SourceHealth


@pytest.mark.asyncio
async def test_catalog_adopts_legacy_source_and_preserves_operator_changes(sqlite_session_factory):
    registry = build_default_registry()
    async with sqlite_session_factory() as session:
        legacy = JobSource(
            name="Operator's name",
            adapter_type="rabota_md",
            base_url="https://www.rabota.md/",
            configuration={"operator_setting": "keep"},
            enabled=True,
            health_status=SourceHealth.HEALTHY,
            automatic_actions_paused=False,
        )
        session.add(legacy)
        await session.commit()
        original_id = legacy.id
        created = await reconcile_source_catalog(session, registry)
        assert set(created) == {
            definition.key
            for definition in registry.source_definitions().values()
            if definition.key != "rabota_md"
        }
        await session.commit()
    # Changing the code's name, URL and defaults must not create a new identity.
    changed = JobSourceAdapterRegistry()
    changed.register(
        "rabota_md",
        FixtureSourceAdapter,
        source=replace(
            registry.source_definitions()["rabota_md"],
            name="Changed code name",
            base_url="https://other.example.test",
            configuration={"operator_setting": "overwrite"},
        ),
    )
    async with sqlite_session_factory() as session:
        assert await reconcile_source_catalog(session, changed) == []
        await session.commit()
        legacy = await session.get(JobSource, original_id)
        assert legacy is not None and legacy.catalog_key == "rabota_md"
        assert legacy.name == "Operator's name"
        assert legacy.base_url == "https://www.rabota.md/"
        assert legacy.configuration == {"operator_setting": "keep"}
        assert legacy.enabled and not legacy.automatic_actions_paused
        assert legacy.health_status == SourceHealth.HEALTHY
        assert await session.scalar(select(func.count(JobSource.id))) == len(
            registry.source_definitions()
        )
        assert await session.scalar(select(func.count(UserProfile.id))) == 0


@pytest.mark.asyncio
async def test_changed_registry_adds_new_site_without_hardcoded_seed(sqlite_session_factory):
    registry = build_default_registry()
    async with sqlite_session_factory() as session:
        await reconcile_source_catalog(session, registry)
        await session.commit()
    registry.register(
        "new_board",
        FixtureSourceAdapter,
        source=SourceDefinition(
            key="stable-new-board", name="New board", base_url="https://new.example.test"
        ),
    )
    async with sqlite_session_factory() as session:
        assert await reconcile_source_catalog(session, registry) == ["stable-new-board"]
        await session.commit()
        assert await reconcile_source_catalog(session, registry) == []
        source = await session.scalar(
            select(JobSource).where(JobSource.catalog_key == "stable-new-board")
        )
        assert source is not None
        assert source.adapter_type == "new_board"
        assert source.enabled is False
        assert source.automatic_actions_paused is True
        assert source.health_status == SourceHealth.PAUSED
        assert await session.scalar(select(func.count(JobSource.id))) == len(
            registry.source_definitions()
        )


def test_registry_rejects_duplicate_stable_source_keys():
    registry = JobSourceAdapterRegistry()
    definition = SourceDefinition(
        key="same-site", name="Board", base_url="https://board.example.test"
    )
    registry.register("first", FixtureSourceAdapter, source=definition)
    with pytest.raises(AdapterRegistryError, match="duplicate source catalog key"):
        registry.register("second", FixtureSourceAdapter, source=definition)


@pytest.mark.asyncio
async def test_startup_does_not_reacknowledge_an_existing_unreviewed_source(sqlite_session_factory):
    registry = build_default_registry()
    async with sqlite_session_factory() as session:
        await reconcile_source_catalog(session, registry)
        await session.commit()
        source = await session.scalar(
            select(JobSource).where(JobSource.catalog_key == "delucru_md")
        )
        source.configuration = {
            "policy_review_acknowledged": False,
            "policy_review_reference": None,
        }
        original_id = source.id
        await session.commit()
    async with sqlite_session_factory() as session:
        await reconcile_source_catalog(session, registry)
        await session.commit()
        source = await session.get(JobSource, original_id)
        assert source.configuration == {
            "policy_review_acknowledged": False,
            "policy_review_reference": None,
        }
        assert source.enabled is False and source.automatic_actions_paused is True
