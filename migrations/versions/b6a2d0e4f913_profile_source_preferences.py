"""add per-profile source preferences

Revision ID: b6a2d0e4f913
Revises: d4b9a7c2e611
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6a2d0e4f913"
down_revision: str | None = "d4b9a7c2e611"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "profile_source_preferences",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["user_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["job_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("profile_id", "source_id"),
    )


def downgrade() -> None:
    op.drop_table("profile_source_preferences")
