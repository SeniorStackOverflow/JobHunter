"""MCP exposes the per-source category choice; the profile-wide lists are a
read-only mirror and can no longer be patched directly."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.entities import ProfileSourcePreference, SourceCategory
from app.profiles.schemas import JobPreferenceUpdateInput
from tests.unit.test_policy_and_email import make_graph


async def _seed(session_factory, storage: Path) -> tuple[str, str]:
    async with session_factory() as session:
        graph = await make_graph(session, storage)
        source, profile = graph[0], graph[1]
        profile.is_default = True
        for external_id, name in (("technology", "IT"), ("workers", "Разнорабочие")):
            session.add(
                SourceCategory(
                    source_id=source.id,
                    external_id=external_id,
                    name=name,
                    url=f"{source.base_url}/{external_id}",
                    locale="ru",
                )
            )
        await session.commit()
        return str(source.id), str(profile.id)


async def test_mcp_lists_sources_with_known_categories_and_the_current_choice(
    sqlite_session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.database import session as database_session
    from app.mcp import server as mcp_server

    source_id, profile_id = await _seed(sqlite_session_factory, tmp_path)
    monkeypatch.setattr(database_session, "async_session_factory", sqlite_session_factory)

    result = await mcp_server.get_source_categories(profile_id)

    assert result == [
        {
            "source_id": source_id,
            "source_name": "Fixture",
            "adapter_type": "fixture_source",
            "selected": True,
            "configured": False,
            "search": ["technology"],
            "auto_send": ["technology"],
            "excluded": [],
            "known_categories": [
                {"id": "technology", "name": "IT"},
                {"id": "workers", "name": "Разнорабочие"},
            ],
        }
    ]


async def test_mcp_sets_the_choice_for_one_source(
    sqlite_session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import UUID

    from app.database import session as database_session
    from app.mcp import server as mcp_server

    source_id, profile_id = await _seed(sqlite_session_factory, tmp_path)
    monkeypatch.setattr(database_session, "async_session_factory", sqlite_session_factory)

    result = await mcp_server.set_source_categories(
        source_id, search=["workers"], auto_send=["technology"], excluded=[], profile_id=profile_id
    )

    assert result["search"] == ["technology", "workers"]
    assert result["auto_send"] == ["technology"]
    async with sqlite_session_factory() as session:
        row = await session.get(ProfileSourcePreference, (UUID(profile_id), UUID(source_id)))
        assert row is not None and row.categories_configured is True


async def test_mcp_refuses_a_category_the_source_does_not_publish(
    sqlite_session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.database import session as database_session
    from app.mcp import server as mcp_server

    source_id, profile_id = await _seed(sqlite_session_factory, tmp_path)
    monkeypatch.setattr(database_session, "async_session_factory", sqlite_session_factory)

    with pytest.raises(ValueError, match="unknown source category"):
        await mcp_server.set_source_categories(
            source_id, search=["sklad"], auto_send=[], excluded=[], profile_id=profile_id
        )


def test_profile_wide_category_lists_cannot_be_patched() -> None:
    for field in ("allowed_categories", "auto_send_categories", "forbidden_categories"):
        with pytest.raises(ValidationError):
            JobPreferenceUpdateInput.model_validate({field: ["operations"]})
