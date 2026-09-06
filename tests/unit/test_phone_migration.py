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


@pytest.mark.asyncio
async def test_phase_2b_columns_present_after_metadata_create(sqlite_engine: AsyncEngine) -> None:
    def _cols(sync_conn: object, table: str) -> set[str]:
        return {c["name"] for c in inspect(sync_conn).get_columns(table)}

    async with sqlite_engine.connect() as conn:
        turns = await conn.run_sync(_cols, "communication_turns")
        sessions = await conn.run_sync(_cols, "communication_sessions")

    assert "audio_evidence_path" in turns
    assert {"summary", "summary_state"} <= sessions


async def test_phase_2b_verification_schema_present_after_metadata_create(
    sqlite_engine: AsyncEngine,
) -> None:
    def _details(sync_conn: object, table: str) -> tuple[set[str], list[dict], list[dict]]:
        inspector = inspect(sync_conn)
        columns = {column["name"] for column in inspector.get_columns(table)}
        indexes = inspector.get_indexes(table)
        uniques = inspector.get_unique_constraints(table)
        return columns, indexes, uniques

    async with sqlite_engine.connect() as conn:
        sessions, session_indexes, session_uniques = await conn.run_sync(
            _details, "communication_sessions"
        )
        facts, _, fact_uniques = await conn.run_sync(_details, "call_facts")

    assert {
        "verification_status",
        "verification_revision",
        "processing_started_at",
        "transport_external_id",
        "related_session_id",
    } <= sessions
    assert {"confirmation_source", "confirmed_at"} <= facts
    assert {
        "ix_communication_sessions_verification_status",
        "ix_communication_sessions_related_session_id",
    } <= {index["name"] for index in session_indexes}
    assert "uq_communication_sessions_transport_channel_external_id" in {
        unique["name"] for unique in session_uniques
    }
    assert "uq_call_facts_session_field" in {unique["name"] for unique in fact_uniques}
