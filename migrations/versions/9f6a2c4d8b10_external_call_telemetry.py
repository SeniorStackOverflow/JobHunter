"""external call telemetry

Revision ID: 9f6a2c4d8b10
Revises: 645593917157
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9f6a2c4d8b10"
down_revision: str | None = "645593917157"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_call_events",
        sa.Column("subsystem", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("upstream_service", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("resource", sa.String(length=255), nullable=True),
        sa.Column("logical_request_id", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=255), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.String(length=255), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("provider_error_code", sa.String(length=128), nullable=True),
        sa.Column("exception_type", sa.String(length=128), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("retry_after_seconds", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("recovered", sa.Boolean(), nullable=False),
        sa.Column("is_final_attempt", sa.Boolean(), nullable=False),
        sa.Column("event_metadata", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_external_call_events_occurred_at",
        "external_call_events",
        ["occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_external_call_events_occurred_subsystem",
        "external_call_events",
        ["occurred_at", "subsystem"],
        unique=False,
    )
    op.create_index(
        "ix_external_call_events_logical_request",
        "external_call_events",
        ["logical_request_id"],
        unique=False,
    )
    op.create_index(
        "ix_external_call_events_http_status",
        "external_call_events",
        ["http_status"],
        unique=False,
    )
    op.create_index(
        "ix_external_call_events_provider_error_code",
        "external_call_events",
        ["provider_error_code"],
        unique=False,
    )
    op.create_index(
        "ix_external_call_events_exception_type",
        "external_call_events",
        ["exception_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_external_call_events_exception_type", table_name="external_call_events")
    op.drop_index("ix_external_call_events_provider_error_code", table_name="external_call_events")
    op.drop_index("ix_external_call_events_http_status", table_name="external_call_events")
    op.drop_index("ix_external_call_events_logical_request", table_name="external_call_events")
    op.drop_index("ix_external_call_events_occurred_subsystem", table_name="external_call_events")
    op.drop_index("ix_external_call_events_occurred_at", table_name="external_call_events")
    op.drop_table("external_call_events")
