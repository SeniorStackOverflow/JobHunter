"""persist deterministic hard requirement evidence

Revision ID: d1e2f3a4b5c6
Revises: 9f6a2c4d8b10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "9f6a2c4d8b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "match_evaluations",
        sa.Column(
            "hard_requirements",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "match_evaluations",
        sa.Column("hard_requirement_rules_version", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("match_evaluations", "hard_requirement_rules_version")
    op.drop_column("match_evaluations", "hard_requirements")
