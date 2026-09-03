"""communication session phonegate generation

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "phonegate_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.alter_column("phonegate_generation", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.drop_column("phonegate_generation")
