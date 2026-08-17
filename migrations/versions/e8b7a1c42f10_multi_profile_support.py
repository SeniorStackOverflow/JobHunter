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
    op.add_column(
        "user_profiles",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        "UPDATE user_profiles SET is_default = TRUE WHERE id = "
        "(SELECT id FROM user_profiles ORDER BY created_at, id LIMIT 1)"
    )
    op.alter_column("user_profiles", "is_default", server_default=None)

    for table in ("job_preferences", "resumes", "match_evaluations", "applications"):
        op.add_column(table, sa.Column("profile_id", sa.Uuid(), nullable=True))
        op.execute(
            f"UPDATE {table} SET profile_id = "
            "(SELECT id FROM user_profiles WHERE is_default = TRUE ORDER BY created_at, id LIMIT 1)"
        )
        op.alter_column(table, "profile_id", nullable=False)
        op.create_foreign_key(
            op.f(f"fk_{table}_profile_id_user_profiles"),
            table,
            "user_profiles",
            ["profile_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index(op.f(f"ix_{table}_profile_id"), table, ["profile_id"], unique=False)

    op.create_unique_constraint(
        "uq_job_preference_profile", "job_preferences", ["profile_id"]
    )
    op.drop_constraint("uq_resumes_sha256", "resumes", type_="unique")
    op.create_unique_constraint("uq_resume_profile_sha256", "resumes", ["profile_id", "sha256"])
    op.drop_constraint("uq_application_canonical_job", "applications", type_="unique")
    op.create_unique_constraint(
        "uq_application_profile_canonical_job",
        "applications",
        ["profile_id", "canonical_job_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_application_profile_canonical_job", "applications", type_="unique"
    )
    op.create_unique_constraint(
        "uq_application_canonical_job", "applications", ["canonical_job_id"]
    )
    op.drop_constraint("uq_resume_profile_sha256", "resumes", type_="unique")
    op.create_unique_constraint("uq_resumes_sha256", "resumes", ["sha256"])
    op.drop_constraint("uq_job_preference_profile", "job_preferences", type_="unique")

    for table in ("applications", "match_evaluations", "resumes", "job_preferences"):
        op.drop_index(op.f(f"ix_{table}_profile_id"), table_name=table)
        op.drop_constraint(
            op.f(f"fk_{table}_profile_id_user_profiles"), table, type_="foreignkey"
        )
        op.drop_column(table, "profile_id")

    op.drop_column("user_profiles", "is_default")
