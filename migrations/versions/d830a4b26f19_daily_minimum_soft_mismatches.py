"""Persist explicit soft matching gaps for bounded daily-minimum catch-up.

Revision ID: d830a4b26f19
Revises: c72f9a31d5be
"""

import sqlalchemy as sa
from alembic import op

revision = "d830a4b26f19"
down_revision = "c72f9a31d5be"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "match_evaluations",
        sa.Column("soft_mismatches", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "match_evaluations",
        sa.Column(
            "optional_requirements_missing",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("match_evaluations", "optional_requirements_missing")
    op.drop_column("match_evaluations", "soft_mismatches")
