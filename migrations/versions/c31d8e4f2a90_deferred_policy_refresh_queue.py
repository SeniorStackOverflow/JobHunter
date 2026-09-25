"""add deferred policy refresh queue

Revision ID: c31d8e4f2a90
Revises: a91c4e72b6d5
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "c31d8e4f2a90"
down_revision: str | None = "a91c4e72b6d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "application_policy_refresh_queue",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("employer_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["user_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employer_id"], ["canonical_employers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "employer_id",
            name="uq_application_policy_refresh_profile_employer",
        ),
    )
    op.create_index(
        "ix_application_policy_refresh_enqueued",
        "application_policy_refresh_queue",
        ["enqueued_at", "id"],
        unique=False,
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT DISTINCT profile_id, employer_id
            FROM applications
            WHERE status = 'deferred'
              AND policy_decision = 'deferred'
              AND employer_id IS NOT NULL
            """
        )
    ).all()
    queue = sa.table(
        "application_policy_refresh_queue",
        sa.column("id", sa.Uuid()),
        sa.column("profile_id", sa.Uuid()),
        sa.column("employer_id", sa.Uuid()),
        sa.column("reason", sa.String(length=128)),
        sa.column("enqueued_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    if rows:
        bind.execute(
            queue.insert(),
            [
                {
                    "id": uuid4(),
                    "profile_id": row.profile_id,
                    "employer_id": row.employer_id,
                    "reason": "migration_seed",
                    "enqueued_at": now,
                }
                for row in rows
            ],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_application_policy_refresh_enqueued",
        table_name="application_policy_refresh_queue",
    )
    op.drop_table("application_policy_refresh_queue")
