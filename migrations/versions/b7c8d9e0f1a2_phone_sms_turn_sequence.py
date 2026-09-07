"""make one canonical turn sequence per communication session

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: str | None = "a6b7c8d9e0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _duplicate_count(bind: sa.Connection) -> int:
    return int(
        bind.execute(
            sa.text(
                """
                SELECT COUNT(*) FROM (
                    SELECT session_id, seq
                    FROM communication_turns
                    GROUP BY session_id, seq
                    HAVING COUNT(*) > 1
                ) AS duplicate_groups
                """
            )
        ).scalar_one()
    )


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = _duplicate_count(bind)
    if duplicates:
        raise RuntimeError(
            "Cannot add uq_communication_turns_session_seq: duplicate turn sequence "
            "rows exist for (session_id, seq); resolve these rows before retrying. "
            f"Duplicate groups: {duplicates}"
        )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("communication_turns") as batch_op:
            batch_op.create_unique_constraint(
                "uq_communication_turns_session_seq", ["session_id", "seq"]
            )
    else:
        op.create_unique_constraint(
            "uq_communication_turns_session_seq",
            "communication_turns",
            ["session_id", "seq"],
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("communication_turns") as batch_op:
            batch_op.drop_constraint("uq_communication_turns_session_seq", type_="unique")
    else:
        op.drop_constraint(
            "uq_communication_turns_session_seq", "communication_turns", type_="unique"
        )
