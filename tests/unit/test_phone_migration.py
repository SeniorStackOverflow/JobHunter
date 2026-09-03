from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

import app.models  # noqa: F401  (registers every ORM entity on ``Base.metadata``)


@pytest.mark.asyncio
async def test_phone_tables_present_after_metadata_create(sqlite_engine: AsyncEngine) -> None:
    def _tables(sync_conn: object) -> set[str]:
        return set(inspect(sync_conn).get_table_names())

    async with sqlite_engine.connect() as conn:
        names = await conn.run_sync(_tables)

    assert {
        "communication_sessions",
        "communication_turns",
        "call_facts",
        "interview_appointments",
        "phone_channel_health",
    } <= names


@pytest.mark.asyncio
async def test_phase_2a_columns_present_after_metadata_create(sqlite_engine: AsyncEngine) -> None:
    def _cols(sync_conn: object, table: str) -> set[str]:
        return {c["name"] for c in inspect(sync_conn).get_columns(table)}

    async with sqlite_engine.connect() as conn:
        turns = await conn.run_sync(_cols, "communication_turns")
        sessions = await conn.run_sync(_cols, "communication_sessions")

    assert {"delivery_status", "spoken_text"} <= turns
    assert {"auto_answered", "script_stage"} <= sessions
