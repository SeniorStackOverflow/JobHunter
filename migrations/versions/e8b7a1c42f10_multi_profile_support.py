"""add multi profile support

Revision ID: e8b7a1c42f10
Revises: d2e7c1a4b903
Create Date: 2026-08-15 16:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b7a1c42f10"
down_revision: str | None = "d2e7c1a4b903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_profiles") as batch_op:
        batch_op.add_column(
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.execute(
        "UPDATE user_profiles SET is_default = TRUE WHERE id = "
        "(SELECT id FROM user_profiles ORDER BY created_at, id LIMIT 1)"
    )
    with op.batch_alter_table("user_profiles") as batch_op:
        batch_op.alter_column("is_default", server_default=None)

    for table in ("job_preferences", "resumes", "match_evaluations", "applications"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("profile_id", sa.Uuid(), nullable=True))
        op.execute(
            f"UPDATE {table} SET profile_id = "
            "(SELECT id FROM user_profiles WHERE is_default = TRUE ORDER BY created_at, id LIMIT 1)"
        )
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column("profile_id", nullable=False)
            batch_op.create_foreign_key(
                op.f(f"fk_{table}_profile_id_user_profiles"),
                "user_profiles",
                ["profile_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch_op.create_index(op.f(f"ix_{table}_profile_id"), ["profile_id"], unique=False)

    with op.batch_alter_table("job_preferences") as batch_op:
        batch_op.create_unique_constraint("uq_job_preference_profile", ["profile_id"])
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.drop_constraint("uq_resumes_sha256", type_="unique")
        batch_op.create_unique_constraint("uq_resume_profile_sha256", ["profile_id", "sha256"])
    with op.batch_alter_table("applications") as batch_op:
        batch_op.drop_constraint("uq_application_canonical_job", type_="unique")
        batch_op.create_unique_constraint(
            "uq_application_profile_canonical_job", ["profile_id", "canonical_job_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("applications") as batch_op:
        batch_op.drop_constraint("uq_application_profile_canonical_job", type_="unique")
        batch_op.create_unique_constraint("uq_application_canonical_job", ["canonical_job_id"])
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.drop_constraint("uq_resume_profile_sha256", type_="unique")
        batch_op.create_unique_constraint("uq_resumes_sha256", ["sha256"])
    with op.batch_alter_table("job_preferences") as batch_op:
        batch_op.drop_constraint("uq_job_preference_profile", type_="unique")

    for table in ("applications", "match_evaluations", "resumes", "job_preferences"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_index(op.f(f"ix_{table}_profile_id"))
            batch_op.drop_constraint(
                op.f(f"fk_{table}_profile_id_user_profiles"), type_="foreignkey"
            )
            batch_op.drop_column("profile_id")

    with op.batch_alter_table("user_profiles") as batch_op:
        batch_op.drop_column("is_default")
