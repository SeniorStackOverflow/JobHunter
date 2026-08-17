"""bind applications to exact source evaluations

Revision ID: 9c4e7d2a81f0
Revises: 7b91e0a4f6cd
Create Date: 2026-08-03 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c4e7d2a81f0"
down_revision: str | None = "7b91e0a4f6cd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing evaluations intentionally remain NULL. They predate the content
    # binding and therefore must be re-run before they can authorize delivery.
    with op.batch_alter_table("match_evaluations") as batch_op:
        batch_op.add_column(
            sa.Column("source_content_hash", sa.String(length=64), nullable=True),
        )
        batch_op.add_column(sa.Column("resume_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("resume_sha256", sa.String(length=64), nullable=True),
        )
        batch_op.add_column(
            sa.Column("profile_fingerprint", sa.String(length=64), nullable=True),
        )
        batch_op.add_column(
            sa.Column("preference_fingerprint", sa.String(length=64), nullable=True),
        )
        batch_op.add_column(sa.Column("confirmed_fact_hashes", sa.JSON(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_match_evaluations_resume_id_resumes"),
            "resumes",
            ["resume_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("applications") as batch_op:
        batch_op.add_column(
            sa.Column("match_evaluation_id", sa.Uuid(), nullable=True),
        )
        batch_op.create_foreign_key(
            op.f("fk_applications_match_evaluation_id_match_evaluations"),
            "match_evaluations",
            ["match_evaluation_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("applications") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_applications_match_evaluation_id_match_evaluations"),
            type_="foreignkey",
        )
        batch_op.drop_column("match_evaluation_id")
    with op.batch_alter_table("match_evaluations") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_match_evaluations_resume_id_resumes"),
            type_="foreignkey",
        )
        batch_op.drop_column("confirmed_fact_hashes")
        batch_op.drop_column("preference_fingerprint")
        batch_op.drop_column("profile_fingerprint")
        batch_op.drop_column("resume_sha256")
        batch_op.drop_column("resume_id")
        batch_op.drop_column("source_content_hash")
