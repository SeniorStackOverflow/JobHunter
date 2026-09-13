from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import Session

from app.models.entities import CommunicationSession, Resume, UserProfile
from app.models.enums import CommunicationChannel, CommunicationDirection
from app.settings import get_settings


def test_fresh_sqlite_database_migrations_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "fresh.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_settings.cache_clear()

    with closing(sqlite3.connect(database_path)) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision == ("9f6a2c4d8b10",)

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        database = inspect(engine)
        assert {"public_emails", "public_phones", "matching_content_hash"} <= {
            column["name"] for column in database.get_columns("source_jobs")
        }
        resume_columns = {column["name"]: column for column in database.get_columns("resumes")}
        assert "archived" in resume_columns
        assert resume_columns["archived"]["nullable"] is False
        assert "requires_rematch" in {
            column["name"] for column in database.get_columns("job_snapshots")
        }
        assert "source_matching_hash" in {
            column["name"] for column in database.get_columns("match_evaluations")
        }
        assert "profile_id" in {column["name"] for column in database.get_columns("applications")}
        assert "phonegate_generation" in {
            column["name"] for column in database.get_columns("communication_sessions")
        }
        assert {"delivery_status", "spoken_text"} <= {
            column["name"] for column in database.get_columns("communication_turns")
        }
        assert {"auto_answered", "script_stage"} <= {
            column["name"] for column in database.get_columns("communication_sessions")
        }
        assert "audio_evidence_path" in {
            column["name"] for column in database.get_columns("communication_turns")
        }
        assert {"summary", "summary_state"} <= {
            column["name"] for column in database.get_columns("communication_sessions")
        }
        session_columns = {
            column["name"]: column for column in database.get_columns("communication_sessions")
        }
        assert session_columns["phonegate_event_id_start"]["nullable"] is True
        assert session_columns["transport_external_id"]["nullable"] is True
        assert session_columns["related_session_id"]["nullable"] is True
        assert session_columns["processing_started_at"]["nullable"] is True
        assert session_columns["verification_revision"]["nullable"] is False
        assert session_columns["verification_status"]["nullable"] is False
        assert {
            "verification_status",
            "verification_revision",
            "processing_started_at",
            "transport_external_id",
            "related_session_id",
        } <= {column["name"] for column in database.get_columns("communication_sessions")}
        assert {"confirmation_source", "confirmed_at"} <= {
            column["name"] for column in database.get_columns("call_facts")
        }
        assert "ix_communication_sessions_verification_status" in {
            index["name"] for index in database.get_indexes("communication_sessions")
        }
        assert "ix_communication_sessions_related_session_id" in {
            index["name"] for index in database.get_indexes("communication_sessions")
        }
        assert "uq_communication_sessions_transport_channel_external_id" in {
            constraint["name"]
            for constraint in database.get_unique_constraints("communication_sessions")
        }
        assert "uq_call_facts_session_field" in {
            constraint["name"] for constraint in database.get_unique_constraints("call_facts")
        }
        assert "uq_communication_turns_session_seq" in {
            constraint["name"]
            for constraint in database.get_unique_constraints("communication_turns")
        }
        assert {
            "review_feedback_events",
            "review_learning_settings",
            "communication_sessions",
            "communication_turns",
            "call_facts",
            "interview_appointments",
            "phone_channel_health",
            "phone_device_snapshot",
        } <= set(database.get_table_names())
    finally:
        engine.dispose()

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with Session(engine) as session:
            profile = UserProfile(name="Migration profile", is_default=True)
            session.add(profile)
            session.flush()
            resume = Resume(
                profile_id=profile.id,
                name="Migration resume",
                category="ops",
                storage_key="migration-resume.pdf",
                original_filename="migration-resume.pdf",
                mime_type="application/pdf",
                sha256="0" * 64,
            )
            session.add(resume)
            session.flush()
            assert resume.archived is False
            started_at = datetime.now(UTC)
            session.add(
                CommunicationSession(
                    profile_id=profile.id,
                    channel=CommunicationChannel.CALL,
                    transport="phonegate",
                    direction=CommunicationDirection.INBOUND,
                    phonegate_event_id_start=1,
                    started_at=started_at,
                )
            )
            session.add(
                CommunicationSession(
                    profile_id=profile.id,
                    channel=CommunicationChannel.SMS,
                    transport="phonegate",
                    direction=CommunicationDirection.INBOUND,
                    remote_address="+37360000000",
                    remote_raw="+37360000000",
                    phonegate_event_id_start=None,
                    transport_external_id="incoming:1720000000000:+37360000000",
                    started_at=started_at,
                    ended_at=started_at,
                )
            )
            session.commit()
            assert session.query(CommunicationSession).count() == 2
    finally:
        engine.dispose()

    # The claim-token migration is independently reversible and must preserve
    # the SMS row while moving to the preceding verification schema.
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        command.downgrade(Config("alembic.ini"), "f2a3b4c5d6e7")
        with closing(sqlite3.connect(database_path)) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM communication_sessions WHERE channel = 'sms'"
            ).fetchone() == (1,)
            assert "claim_token" not in {
                row[1] for row in connection.execute("PRAGMA table_info(communication_sessions)")
            }
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("DELETE FROM communication_sessions WHERE channel = 'sms'")
            connection.commit()
    finally:
        engine.dispose()

    get_settings.cache_clear()
    try:
        command.downgrade(Config("alembic.ini"), "base")
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_settings.cache_clear()

    with closing(sqlite3.connect(database_path)) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision == ("9f6a2c4d8b10",)
