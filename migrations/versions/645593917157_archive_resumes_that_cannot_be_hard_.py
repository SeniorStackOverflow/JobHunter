"""archive resumes that cannot be hard-deleted

Revision ID: 645593917157
Revises: b7c8d9e0f1a2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "645593917157"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive, no data change: existing resumes are not archived. The temporary
    # server default backfills the NOT NULL column for existing rows, then is
    # dropped so the column matches the ORM model (Python-side default only).
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.add_column(
            sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.alter_column("archived", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.drop_column("archived")
