"""employer relationship memory and post-send delivery state

Revision ID: ef3a1b2c4d5e
Revises: d1e2f3a4b5c6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ef3a1b2c4d5e"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_employer_fk(table: str) -> None:
    with op.batch_alter_table(table) as batch_op:
        batch_op.add_column(sa.Column("employer_id", sa.Uuid(), nullable=True))
        batch_op.create_index(f"ix_{table}_employer_id", ["employer_id"])
        batch_op.create_foreign_key(
            f"fk_{table}_employer_id_canonical_employers",
            "canonical_employers",
            ["employer_id"],
            ["id"],
            ondelete="SET NULL",
        )


def upgrade() -> None:
    with op.batch_alter_table("email_deliveries") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=17),
            type_=sa.String(length=18),
            existing_nullable=False,
        )
    op.create_table(
        "canonical_employers",
        sa.Column("normalized_name", sa.String(length=500), nullable=False),
        sa.Column("primary_domain", sa.String(length=255), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_canonical_employers_primary_domain",
        "canonical_employers",
        ["primary_domain"],
    )
    op.create_table(
        "employer_identifiers",
        sa.Column("employer_id", sa.Uuid(), nullable=False),
        sa.Column("identifier_type", sa.String(length=20), nullable=False),
        sa.Column("namespace", sa.String(length=128), nullable=False),
        sa.Column("normalized_value", sa.String(length=2048), nullable=False),
        sa.Column("raw_value", sa.String(length=2048), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["employer_id"], ["canonical_employers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_employer_identifiers_employer", "employer_identifiers", ["employer_id"])
    op.create_index(
        "ix_employer_identifiers_lookup",
        "employer_identifiers",
        ["identifier_type", "normalized_value"],
    )
    op.create_index(
        "uq_employer_identifiers_strong",
        "employer_identifiers",
        ["identifier_type", "namespace", "normalized_value"],
        unique=True,
        postgresql_where=sa.text("identifier_type <> 'normalized_name'"),
        sqlite_where=sa.text("identifier_type <> 'normalized_name'"),
    )

    for table in (
        "canonical_jobs",
        "source_jobs",
        "employer_contacts",
        "applications",
        "communication_sessions",
        "interview_appointments",
    ):
        _add_employer_fk(table)

    op.create_table(
        "employer_identity_candidates",
        sa.Column("source_job_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_employer_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_employer_ids", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["assigned_employer_id"], ["canonical_employers.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["source_job_id"], ["source_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_job_id", name="uq_employer_identity_candidate_source_job"),
    )

    op.create_table(
        "employer_interaction_events",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("employer_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("canonical_job_id", sa.Uuid(), nullable=True),
        sa.Column("source_job_id", sa.Uuid(), nullable=True),
        sa.Column("communication_session_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("channel", sa.String(length=11), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("suppression_scope", sa.String(length=11), nullable=False),
        sa.Column("role_family", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["canonical_job_id"], ["canonical_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["communication_session_id"],
            ["communication_sessions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["employer_id"], ["canonical_employers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["user_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_job_id"], ["source_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["turn_id"], ["communication_turns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_employer_interaction_event_idempotency"),
    )
    op.create_index(
        "ix_employer_interaction_events_relationship",
        "employer_interaction_events",
        ["profile_id", "employer_id", "occurred_at"],
    )
    op.create_table(
        "employer_relationships",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("employer_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=18), nullable=False),
        sa.Column("last_interaction_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_application_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_interview_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suppression_scope", sa.String(length=11), nullable=False),
        sa.Column("suppression_reason", sa.String(length=255), nullable=True),
        sa.Column("suppressed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suppressed_by_event_id", sa.Uuid(), nullable=True),
        sa.Column("suppressed_canonical_job_id", sa.Uuid(), nullable=True),
        sa.Column("suppressed_role_family", sa.String(length=255), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["employer_id"], ["canonical_employers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["user_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["suppressed_by_event_id"],
            ["employer_interaction_events.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["suppressed_canonical_job_id"],
            ["canonical_jobs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_id", "employer_id", name="uq_employer_relationship"),
    )
    op.create_index("ix_employer_relationship_state", "employer_relationships", ["state"])

    with op.batch_alter_table("employer_contacts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "delivery_state",
                sa.String(length=17),
                nullable=False,
                server_default="unknown",
            )
        )
        batch_op.add_column(
            sa.Column("last_delivery_attempt_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_delivery_failure_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("last_smtp_status", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("last_failure_reason", sa.String(length=128), nullable=True))
    with op.batch_alter_table("employer_contacts") as batch_op:
        batch_op.alter_column("delivery_state", server_default=None)
        batch_op.alter_column("failure_count", server_default=None)

    delivery_columns = (
        sa.Column("rfc_message_id", sa.String(length=998), nullable=True),
        sa.Column("subject_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("final_recipient", sa.String(length=320), nullable=True),
        sa.Column("smtp_status", sa.String(length=32), nullable=True),
        sa.Column("failure_class", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bounced_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in delivery_columns:
        op.add_column("email_deliveries", column)
    op.create_index("ix_email_deliveries_rfc_message_id", "email_deliveries", ["rfc_message_id"])
    op.create_index("ix_email_deliveries_smtp_status", "email_deliveries", ["smtp_status"])
    op.create_index("ix_email_deliveries_failure_class", "email_deliveries", ["failure_class"])
    op.create_index("ix_email_deliveries_next_retry_at", "email_deliveries", ["next_retry_at"])
    op.execute(
        "UPDATE email_deliveries SET final_recipient = recipient, submitted_at = created_at, "
        "provider_accepted_at = CASE WHEN status = 'sent' THEN updated_at ELSE NULL END, "
        "last_attempt_at = updated_at"
    )

    op.create_table(
        "email_delivery_events",
        sa.Column("delivery_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=False),
        sa.Column("provider_thread_id", sa.String(length=255), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("original_message_id", sa.String(length=998), nullable=True),
        sa.Column("final_recipient", sa.String(length=320), nullable=True),
        sa.Column("smtp_status", sa.String(length=32), nullable=True),
        sa.Column("failure_class", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("permanent", sa.Boolean(), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("safe_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["delivery_id"], ["email_deliveries.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "provider_message_id", name="uq_email_delivery_event_provider_message"
        ),
    )
    op.create_index(
        "ix_email_delivery_events_delivery_id", "email_delivery_events", ["delivery_id"]
    )
    op.create_index(
        "ix_email_delivery_events_delivery_occurred",
        "email_delivery_events",
        ["delivery_id", "occurred_at"],
    )
    op.create_index(
        "ix_email_delivery_events_original_message_id",
        "email_delivery_events",
        ["original_message_id"],
    )
    op.create_index(
        "ix_email_delivery_events_final_recipient",
        "email_delivery_events",
        ["final_recipient"],
    )
    op.create_table(
        "email_mailbox_cursors",
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("history_id", sa.String(length=64), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider"),
    )


def downgrade() -> None:
    op.drop_table("email_mailbox_cursors")
    op.drop_index("ix_email_delivery_events_final_recipient", table_name="email_delivery_events")
    op.drop_index(
        "ix_email_delivery_events_original_message_id", table_name="email_delivery_events"
    )
    op.drop_index("ix_email_delivery_events_delivery_occurred", table_name="email_delivery_events")
    op.drop_index("ix_email_delivery_events_delivery_id", table_name="email_delivery_events")
    op.drop_table("email_delivery_events")
    for index in (
        "ix_email_deliveries_next_retry_at",
        "ix_email_deliveries_failure_class",
        "ix_email_deliveries_smtp_status",
        "ix_email_deliveries_rfc_message_id",
    ):
        op.drop_index(index, table_name="email_deliveries")
    for column in (
        "bounced_at",
        "next_retry_at",
        "last_attempt_at",
        "provider_accepted_at",
        "submitted_at",
        "failure_reason",
        "failure_class",
        "smtp_status",
        "final_recipient",
        "subject_fingerprint",
        "rfc_message_id",
    ):
        op.drop_column("email_deliveries", column)
    op.execute(
        "UPDATE email_deliveries SET status = CASE "
        "WHEN status IN ('submitted', 'provider_accepted', 'delivered') THEN 'sent' "
        "WHEN status IN ('bounced_transient', 'mailbox_full') THEN 'temporary_failure' "
        "WHEN status NOT IN ('sending', 'sent', 'delivery_unknown', "
        "'temporary_failure', 'permanent_failure') THEN 'permanent_failure' "
        "ELSE status END"
    )
    with op.batch_alter_table("email_deliveries") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=18),
            type_=sa.String(length=17),
            existing_nullable=False,
        )
    for column in (
        "last_failure_reason",
        "failure_count",
        "last_smtp_status",
        "last_delivery_failure_at",
        "last_delivery_attempt_at",
        "delivery_state",
    ):
        op.drop_column("employer_contacts", column)
    op.drop_index("ix_employer_relationship_state", table_name="employer_relationships")
    op.drop_table("employer_relationships")
    op.drop_index(
        "ix_employer_interaction_events_relationship",
        table_name="employer_interaction_events",
    )
    op.drop_table("employer_interaction_events")
    op.drop_table("employer_identity_candidates")
    for table in reversed(
        (
            "canonical_jobs",
            "source_jobs",
            "employer_contacts",
            "applications",
            "communication_sessions",
            "interview_appointments",
        )
    ):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(
                f"fk_{table}_employer_id_canonical_employers", type_="foreignkey"
            )
            batch_op.drop_index(f"ix_{table}_employer_id")
            batch_op.drop_column("employer_id")
    op.drop_index("uq_employer_identifiers_strong", table_name="employer_identifiers")
    op.drop_index("ix_employer_identifiers_lookup", table_name="employer_identifiers")
    op.drop_index("ix_employer_identifiers_employer", table_name="employer_identifiers")
    op.drop_table("employer_identifiers")
    op.drop_index("ix_canonical_employers_primary_domain", table_name="canonical_employers")
    op.drop_table("canonical_employers")
