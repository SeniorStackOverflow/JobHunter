"""add scan lifecycle ownership and heartbeat

Revision ID: b7e3c9a1d4f2
Revises: c31d8e4f2a90
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e3c9a1d4f2"
down_revision: str | None = "c31d8e4f2a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scan_runs", sa.Column("owner_task_id", sa.String(length=255), nullable=True))
    op.add_column(
        "scan_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_scan_runs_status_heartbeat",
        "scan_runs",
        ["status", "heartbeat_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scan_runs_status_heartbeat", table_name="scan_runs")
    op.drop_column("scan_runs", "heartbeat_at")
    op.drop_column("scan_runs", "owner_task_id")
