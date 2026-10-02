from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
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
    expected_revision = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_settings.cache_clear()

    with closing(sqlite3.connect(database_path)) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision == (expected_revision,)

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        database = inspect(engine)
        assert {"public_emails", "public_phones", "matching_content_hash"} <= {
            column["name"] for column in database.get_columns("source_jobs")
        }
        assert {
            "canonical_employers",
            "employer_identifiers",
            "employer_interaction_events",
            "employer_relationships",
            "email_delivery_events",
            "email_mailbox_cursors",
            "accounts",
            "account_identities",
            "invites",
            "profile_source_preferences",
        } <= set(database.get_table_names())
        assert {
            "rfc_message_id",
            "subject_fingerprint",
            "final_recipient",
            "smtp_status",
            "failure_class",
            "bounced_at",
        } <= {column["name"] for column in database.get_columns("email_deliveries")}
        resume_columns = {column["name"]: column for column in database.get_columns("resumes")}
        assert "archived" in resume_columns
        assert resume_columns["archived"]["nullable"] is False
        assert "requires_rematch" in {
            column["name"] for column in database.get_columns("job_snapshots")
        }
        match_columns = {column["name"] for column in database.get_columns("match_evaluations")}
        assert "source_matching_hash" in match_columns
        assert {"hard_requirements", "hard_requirement_rules_version"} <= match_columns
        assert {"owner_account_id", "status"} <= {
            column["name"] for column in database.get_columns("user_profiles")
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
    assert revision == (expected_revision,)


def test_send_attempt_ledger_backfill_preserves_hard_maximum_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.entities import EmailDelivery
    from app.models.enums import DeliveryStatus
    from tests.unit.test_daily_minimum import additional_candidate
    from tests.unit.test_policy_and_email import make_graph

    database_path = tmp_path / "ledger.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "d830a4b26f19")
    finally:
        get_settings.cache_clear()

    submitted = datetime(2026, 9, 29, 21, 30, tzinfo=UTC)  # 30 Sept in Chisinau

    async def seed() -> dict[str, str]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                graph = await make_graph(session, tmp_path / "storage")
                graphs = [graph]
                for company in ("Second", "Third", "Fourth"):
                    graphs.append(
                        await additional_candidate(session, graph, company=company, score=90)
                    )
                states = [
                    (DeliveryStatus.DOMAIN_REJECTED, submitted),
                    (DeliveryStatus.TEMPORARY_FAILURE, None),
                    (DeliveryStatus.DELIVERY_UNKNOWN, None),
                    (DeliveryStatus.SENDING, None),
                ]
                identifiers: dict[str, str] = {}
                for item, (status, submitted_at) in zip(graphs, states, strict=True):
                    delivery = EmailDelivery(
                        application_id=item[-1].id,
                        provider="gmail",
                        recipient="jobs@example.com",
                        status=status,
                        sanitized_provider_response={},
                        attempt_count=2 if status is DeliveryStatus.DOMAIN_REJECTED else 1,
                        submitted_at=submitted_at,
                        provider_accepted_at=submitted_at,
                        last_attempt_at=submitted,
                        created_at=submitted,
                    )
                    session.add(delivery)
                    await session.flush()
                    identifiers[status.value] = delivery.id.hex
                await session.commit()
                return identifiers
        finally:
            await engine.dispose()

    # The ORM seeds with head models; add later, unrelated evaluation columns
    # so the insert works at this older revision. The ledger migration reads
    # only email_deliveries/applications.
    with closing(sqlite3.connect(database_path)) as connection:
        for column, kind in (
            ("llm_logical_request_id", "VARCHAR(128)"),
            ("llm_outcome", "VARCHAR(32)"),
            ("llm_failure_code", "VARCHAR(128)"),
            ("llm_failure_path", "VARCHAR(255)"),
            ("llm_attempts", "INTEGER"),
        ):
            connection.execute(f"ALTER TABLE match_evaluations ADD COLUMN {column} {kind}")
        connection.commit()
    identifiers = asyncio.run(seed())

    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "a3e9c2d7b4f1")
    finally:
        get_settings.cache_clear()

    with closing(sqlite3.connect(database_path)) as connection:
        rows = {
            delivery_id: (attempt_no, local_day, outcome)
            for delivery_id, attempt_no, local_day, outcome in connection.execute(
                "SELECT delivery_id, attempt_no, local_day, outcome FROM email_send_attempts"
            )
        }
    assert rows == {
        identifiers["domain_rejected"]: (2, "2026-09-30", "provider_accepted"),
        identifiers["temporary_failure"]: (1, "2026-09-30", "not_transmitted"),
        identifiers["delivery_unknown"]: (1, "2026-09-30", "delivery_unknown"),
        identifiers["sending"]: (1, "2026-09-30", "in_flight"),
    }

    get_settings.cache_clear()
    try:
        command.downgrade(Config("alembic.ini"), "d830a4b26f19")
    finally:
        get_settings.cache_clear()
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM email_deliveries").fetchone() == (4,)
        assert "email_send_attempts" not in {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master")
        }


def test_category_lists_are_copied_to_every_source_of_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from app.models.entities import JobPreference, JobSource

    database_path = tmp_path / "categories.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "c5d7a9e1f3b2")
    finally:
        get_settings.cache_clear()

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with Session(engine) as session:
            sources = [
                JobSource(name=name, base_url=f"https://{name}.example", adapter_type="rss")
                for name in ("first", "second")
            ]
            chooser = UserProfile(name="Chooser")
            excluded_second = UserProfile(name="Excluded second")
            blank = UserProfile(name="Blank")
            session.add_all([*sources, chooser, excluded_second, blank])
            session.flush()
            session.add_all(
                [
                    JobPreference(
                        profile_id=chooser.id,
                        allowed_categories=["others", "warehouses"],
                        auto_send_categories=["warehouses"],
                        forbidden_categories=["calls"],
                    ),
                    JobPreference(profile_id=excluded_second.id, allowed_categories=["it"]),
                    JobPreference(profile_id=blank.id),
                ]
            )
            session.commit()
            ids = {
                "first": sources[0].id.hex,
                "second": sources[1].id.hex,
                "chooser": chooser.id.hex,
                "excluded_second": excluded_second.id.hex,
                "blank": blank.id.hex,
            }
    finally:
        engine.dispose()
    with closing(sqlite3.connect(database_path)) as connection:
        # This profile had already excluded the second source.
        connection.execute(
            "INSERT INTO profile_source_preferences "
            "(profile_id, source_id, enabled, created_at, updated_at) "
            "VALUES (?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (ids["excluded_second"], ids["second"]),
        )
        connection.commit()
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "d8e2f4a6b1c3")
    finally:
        get_settings.cache_clear()

    with closing(sqlite3.connect(database_path)) as connection:
        rows = {
            (profile_id, source_id): (
                enabled,
                configured,
                json.loads(search),
                json.loads(auto_send),
                json.loads(excluded),
            )
            for profile_id, source_id, enabled, configured, search, auto_send, excluded in (
                connection.execute(
                    "SELECT profile_id, source_id, enabled, categories_configured, "
                    "search_categories, auto_send_categories, excluded_categories "
                    "FROM profile_source_preferences"
                )
            )
        }
        preference = connection.execute(
            "SELECT allowed_categories FROM job_preferences WHERE profile_id = ?",
            (ids["chooser"],),
        ).fetchone()
    copied = (1, 1, ["others", "warehouses"], ["warehouses"], ["calls"])
    assert rows == {
        (ids["chooser"], ids["first"]): copied,
        (ids["chooser"], ids["second"]): copied,
        (ids["excluded_second"], ids["first"]): (1, 1, ["it"], [], []),
        # The earlier exclusion of the source is kept.
        (ids["excluded_second"], ids["second"]): (0, 1, ["it"], [], []),
    }
    # The profile-wide lists stay untouched: nothing is re-evaluated by the upgrade.
    assert preference is not None and json.loads(preference[0]) == ["others", "warehouses"]

    get_settings.cache_clear()
    try:
        command.downgrade(Config("alembic.ini"), "c5d7a9e1f3b2")
    finally:
        get_settings.cache_clear()
    with closing(sqlite3.connect(database_path)) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(profile_source_preferences)")
        }
    assert "search_categories" not in columns
    assert "enabled" in columns
