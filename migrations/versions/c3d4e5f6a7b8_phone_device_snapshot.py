"""phone device snapshot

Revision ID: c3d4e5f6a7b8
Revises: b259d94e7049
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b259d94e7049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "phone_device_snapshot",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_phone_device_snapshot")),
    )


def downgrade() -> None:
    op.drop_table("phone_device_snapshot")
