from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import create_engine

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

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision == ("c7d4e6f8a912",)

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        database = inspect(engine)
        assert {"public_emails", "public_phones"} <= {
            column["name"] for column in database.get_columns("source_jobs")
        }
        assert "profile_id" in {column["name"] for column in database.get_columns("applications")}
        assert {
            "review_feedback_events",
            "review_learning_settings",
        } <= set(database.get_table_names())
    finally:
        engine.dispose()

    get_settings.cache_clear()
    try:
        command.downgrade(Config("alembic.ini"), "base")
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_settings.cache_clear()

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision == ("c7d4e6f8a912",)
