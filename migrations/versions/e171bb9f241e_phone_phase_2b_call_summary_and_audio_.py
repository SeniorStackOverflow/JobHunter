"""phone phase 2b: call summary and audio evidence

Revision ID: e171bb9f241e
Revises: e5f6a7b8c9d0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e171bb9f241e"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUMMARY_STATE = sa.Enum(
    "not_applicable",
    "pending",
    "done",
    "failed",
    "skipped",
    name="phonesummarystate",
    native_enum=False,
)


def upgrade() -> None:
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.add_column(sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"))
        batch_op.add_column(
            sa.Column(
                "summary_state",
                _SUMMARY_STATE,
                nullable=False,
                server_default="not_applicable",
            )
        )
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.alter_column("summary", server_default=None)
        batch_op.alter_column("summary_state", server_default=None)
    with op.batch_alter_table("communication_turns") as batch_op:
        batch_op.add_column(sa.Column("audio_evidence_path", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("communication_turns") as batch_op:
        batch_op.drop_column("audio_evidence_path")
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.drop_column("summary_state")
        batch_op.drop_column("summary")
