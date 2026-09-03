"""phone phase 2a: assistant-turn delivery + auto-answer session fields

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("communication_turns") as batch_op:
        batch_op.add_column(
            sa.Column(
                "delivery_status",
                sa.Enum(
                    "not_applicable",
                    "attempted",
                    "delivered",
                    "delivery_unknown",
                    "failed",
                    name="turndeliverystatus",
                    native_enum=False,
                ),
                nullable=False,
                server_default="not_applicable",
            )
        )
        batch_op.add_column(sa.Column("spoken_text", sa.Text(), nullable=True))
    with op.batch_alter_table("communication_turns") as batch_op:
        batch_op.alter_column("delivery_status", server_default=None)

    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.add_column(
            sa.Column("auto_answered", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("script_stage", sa.String(length=32), nullable=True))
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.alter_column("auto_answered", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.drop_column("script_stage")
        batch_op.drop_column("auto_answered")
    with op.batch_alter_table("communication_turns") as batch_op:
        batch_op.drop_column("spoken_text")
        batch_op.drop_column("delivery_status")
