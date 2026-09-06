"""add opaque ownership tokens for phone processing leases

Revision ID: a6b7c8d9e0f1
Revises: f2a3b4c5d6e7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6b7c8d9e0f1"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "communication_sessions",
        sa.Column("claim_token", sa.String(length=96), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("communication_sessions", "claim_token")
