from __future__ import annotations

import importlib

import pytest
from sqlalchemy import inspect, text
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
async def test_resume_archived_column_present_after_metadata_create(
    sqlite_engine: AsyncEngine,
) -> None:
    def _columns(sync_conn: object, table: str) -> dict[str, dict]:
        return {column["name"]: column for column in inspect(sync_conn).get_columns(table)}

    async with sqlite_engine.connect() as conn:
        resumes = await conn.run_sync(_columns, "resumes")

    assert "archived" in resumes
    assert resumes["archived"]["nullable"] is False


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


def test_postgresql_upgrade_operations_do_not_drop_communication_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "migrations.versions.f2a3b4c5d6e7_phone_phase_2b_verification_sms"
    )

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def __getattr__(self, name: str):
            def record(*args: object, **kwargs: object) -> None:
                self.calls.append((name, args))

            return record

    recorder = Recorder()
    monkeypatch.setattr(migration, "op", recorder)
    monkeypatch.setattr(migration, "_assert_no_duplicate_transport_ids", lambda _bind: None)
    migration._upgrade_postgresql()

    assert not any(
        name == "drop_table" and args == ("communication_sessions",)
        for name, args in recorder.calls
    )
    assert ("add_column", ("communication_sessions",)) in {
        (name, args[:1]) for name, args in recorder.calls
    }


def test_postgresql_downgrade_replaces_summary_check_before_adding_legacy_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "migrations.versions.f2a3b4c5d6e7_phone_phase_2b_verification_sms"
    )

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def __getattr__(self, name: str):
            def record(*args: object, **kwargs: object) -> None:
                self.calls.append((name, args))

            return record

    recorder = Recorder()
    monkeypatch.setattr(migration, "op", recorder)
    migration._downgrade_postgresql()

    drop_current = next(
        index
        for index, (name, args) in enumerate(recorder.calls)
        if name == "drop_constraint"
        and args[:2] == ("ck_communication_sessions_phonesummarystate", "communication_sessions")
    )
    add_legacy = next(
        index
        for index, (name, args) in enumerate(recorder.calls)
        if name == "execute" and "ADD CONSTRAINT" in str(args[0])
    )
    assert (
        "drop_constraint",
        (migration._RELATED_SESSION_FK, "communication_sessions"),
    ) in {(name, args[:2]) for name, args in recorder.calls}
    assert drop_current < add_legacy


def test_duplicate_transport_diagnostic_redacts_external_id() -> None:
    migration = importlib.import_module(
        "migrations.versions.f2a3b4c5d6e7_phone_phase_2b_verification_sms"
    )

    class Result:
        def scalar_one(self) -> int:
            return 1

    class Bind:
        def execute(self, _statement: object) -> Result:
            return Result()

    with pytest.raises(RuntimeError) as error:
        migration._assert_no_duplicate_transport_ids(Bind())
    message = str(error.value)
    assert "duplicate session transport ID groups" in message
    assert "+37360000000" not in message


def test_phone_summary_legacy_values_exclude_processing_after_direct_downgrade(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    database_path = tmp_path / "summary-downgrade.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            profile_id = "11111111111111111111111111111111"
            session_id = "22222222222222222222222222222222"
            connection.execute(
                text(
                    "INSERT INTO user_profiles "
                    "(id, name, is_default, languages, work_experience, education, skills, "
                    "driving_licences, confirmed_facts, availability, created_at, updated_at) "
                    "VALUES (:id, 'p', 1, '[]', '[]', '[]', '[]', '[]', '[]', '{}', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": profile_id},
            )
            connection.execute(
                text(
                    "INSERT INTO communication_sessions "
                    "(id, profile_id, channel, transport, direction, remote_address, remote_raw, "
                    "phonegate_event_id_start, started_at, needs_review, rx_frame_stats, "
                    "diagnostics, "
                    "created_at, updated_at, phonegate_generation, auto_answered, summary, "
                    "summary_state, verification_status, verification_revision) VALUES "
                    "(:id, :profile_id, 'call', 'phonegate', 'inbound', '', '', 1, "
                    "CURRENT_TIMESTAMP, "
                    "0, '{}', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, 0, '{}', "
                    "'processing', 'pending', 0)"
                ),
                {"id": session_id, "profile_id": profile_id},
            )
        command.downgrade(Config("alembic.ini"), "e171bb9f241e")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT summary_state FROM communication_sessions")
                ).scalar_one()
                == "pending"
            )
            check = inspect(engine).get_check_constraints("communication_sessions")
            assert check and "processing" not in check[0]["sqltext"]
            assert {"summary", "summary_state"} <= {
                column["name"] for column in inspect(engine).get_columns("communication_sessions")
            }
    finally:
        engine.dispose()
