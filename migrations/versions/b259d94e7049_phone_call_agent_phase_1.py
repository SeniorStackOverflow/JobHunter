"""phone call agent phase 1

Revision ID: b259d94e7049
Revises: 5191960d5cc9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b259d94e7049"
down_revision: str | None = "5191960d5cc9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ``ContactType`` gains a ``PHONE`` member in this phase. It is a non-native enum
# (``native_enum=False``) rendered as ``VARCHAR`` with no CHECK constraint, so the
# new value needs no column DDL and is intentionally not touched here.


def upgrade() -> None:
    op.create_table(
        "phone_channel_health",
        sa.Column("component", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "healthy",
                "degraded",
                "unavailable",
                "unknown",
                name="phonecomponentstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("component", name=op.f("pk_phone_channel_health")),
    )
    op.create_table(
        "communication_sessions",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("canonical_job_id", sa.Uuid(), nullable=True),
        sa.Column("source_job_id", sa.Uuid(), nullable=True),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column(
            "channel",
            sa.Enum("call", "sms", name="communicationchannel", native_enum=False),
            nullable=False,
        ),
        sa.Column("transport", sa.String(length=32), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("inbound", "outbound", name="communicationdirection", native_enum=False),
            nullable=False,
        ),
        sa.Column("remote_address", sa.String(length=32), nullable=False),
        sa.Column("remote_raw", sa.String(length=64), nullable=False),
        sa.Column("phonegate_event_id_start", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ringing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(
                "missed",
                "completed",
                "abandoned",
                "unknown",
                name="communicationoutcome",
                native_enum=False,
            ),
            nullable=True,
        ),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column("rx_frame_stats", sa.JSON(), nullable=False),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_communication_sessions_application_id_applications"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_job_id"],
            ["canonical_jobs.id"],
            name=op.f("fk_communication_sessions_canonical_job_id_canonical_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["employer_contacts.id"],
            name=op.f("fk_communication_sessions_contact_id_employer_contacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["user_profiles.id"],
            name=op.f("fk_communication_sessions_profile_id_user_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_job_id"],
            ["source_jobs.id"],
            name=op.f("fk_communication_sessions_source_job_id_source_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_communication_sessions")),
    )
    op.create_index(
        "ix_communication_sessions_ended_at",
        "communication_sessions",
        ["ended_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_communication_sessions_profile_id"),
        "communication_sessions",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_communication_sessions_profile_started",
        "communication_sessions",
        ["profile_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_communication_sessions_remote_started",
        "communication_sessions",
        ["remote_address", "started_at"],
        unique=False,
    )
    op.create_table(
        "communication_turns",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("phonegate_transcript_id", sa.Integer(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column(
            "speaker",
            sa.Enum(
                "employer",
                "assistant",
                "operator",
                "system",
                name="turnspeaker",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("asr_backend", sa.String(length=32), nullable=True),
        sa.Column("asr_confidence", sa.Float(), nullable=True),
        sa.Column("asr_meta", sa.String(length=255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["communication_sessions.id"],
            name=op.f("fk_communication_turns_session_id_communication_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_communication_turns")),
        sa.UniqueConstraint(
            "session_id",
            "phonegate_transcript_id",
            name="uq_communication_turns_session_transcript",
        ),
    )
    op.create_index(
        op.f("ix_communication_turns_session_id"),
        "communication_turns",
        ["session_id"],
        unique=False,
    )
    op.create_table(
        "interview_appointments",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("communication_session_id", sa.Uuid(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "format",
            sa.Enum(
                "onsite",
                "remote",
                "phone",
                "unknown",
                name="interviewformat",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("meeting_url", sa.String(length=2048), nullable=True),
        sa.Column("contact_person", sa.String(length=255), nullable=True),
        sa.Column("preparation", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "proposed",
                "confirmed",
                "needs_review",
                "cancelled",
                name="interviewstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_interview_appointments_application_id_applications"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["communication_session_id"],
            ["communication_sessions.id"],
            name=op.f("fk_interview_appointments_communication_session_id_communication_sessions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["user_profiles.id"],
            name=op.f("fk_interview_appointments_profile_id_user_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_appointments")),
    )
    op.create_index(
        op.f("ix_interview_appointments_profile_id"),
        "interview_appointments",
        ["profile_id"],
        unique=False,
    )
    op.create_table(
        "call_facts",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("source_turn_id", sa.Uuid(), nullable=True),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("raw_expression", sa.Text(), nullable=False),
        sa.Column("normalized_value", sa.String(length=500), nullable=True),
        sa.Column("asr_confidence", sa.Float(), nullable=True),
        sa.Column("llm_confidence", sa.Float(), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "candidate",
                "confirmed",
                "conflict",
                "unknown",
                name="callfactstate",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("confirmed_by_turn_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["confirmed_by_turn_id"],
            ["communication_turns.id"],
            name=op.f("fk_call_facts_confirmed_by_turn_id_communication_turns"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["communication_sessions.id"],
            name=op.f("fk_call_facts_session_id_communication_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_turn_id"],
            ["communication_turns.id"],
            name=op.f("fk_call_facts_source_turn_id_communication_turns"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_call_facts")),
    )
    op.create_index(
        op.f("ix_call_facts_session_id"),
        "call_facts",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_call_facts_session_id"), table_name="call_facts")
    op.drop_table("call_facts")
    op.drop_index(
        op.f("ix_interview_appointments_profile_id"),
        table_name="interview_appointments",
    )
    op.drop_table("interview_appointments")
    op.drop_index(
        op.f("ix_communication_turns_session_id"),
        table_name="communication_turns",
    )
    op.drop_table("communication_turns")
    op.drop_index(
        "ix_communication_sessions_remote_started",
        table_name="communication_sessions",
    )
    op.drop_index(
        "ix_communication_sessions_profile_started",
        table_name="communication_sessions",
    )
    op.drop_index(
        op.f("ix_communication_sessions_profile_id"),
        table_name="communication_sessions",
    )
    op.drop_index(
        "ix_communication_sessions_ended_at",
        table_name="communication_sessions",
    )
    op.drop_table("communication_sessions")
    op.drop_table("phone_channel_health")
