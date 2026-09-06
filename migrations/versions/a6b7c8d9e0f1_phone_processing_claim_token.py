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
    bind = op.get_bind()
    sms_count = int(
        bind.execute(
            sa.text("SELECT COUNT(*) FROM communication_sessions WHERE channel = 'sms'")
        ).scalar_one()
    )
    if sms_count:
        raise RuntimeError("Cannot downgrade phone processing schema while SMS sessions exist")
    op.drop_column("communication_sessions", "claim_token")
