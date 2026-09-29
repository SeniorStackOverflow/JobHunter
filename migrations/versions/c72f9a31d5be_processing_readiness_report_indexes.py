"""gate processing readiness and speed report joins

Revision ID: c72f9a31d5be
Revises: b6a2d0e4f913
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c72f9a31d5be"
down_revision: str | None = "b6a2d0e4f913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BOOTSTRAP_ADMIN_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_index(
        "ix_applications_profile_match_evaluation",
        "applications",
        ["profile_id", "match_evaluation_id"],
        unique=False,
    )
    op.create_index(
        "ix_match_evaluations_profile_created",
        "match_evaluations",
        ["profile_id", "created_at", "id"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE match_evaluations SET "
            "(autovacuum_analyze_scale_factor = 0.02, autovacuum_analyze_threshold = 200)"
        )
    op.execute(
        sa.text(
            """
            UPDATE user_profiles AS profile
            SET status = 'draft', updated_at = CURRENT_TIMESTAMP
            WHERE profile.status = 'active'
              AND replace(CAST(profile.owner_account_id AS TEXT), '-', '') <>
                  replace(:bootstrap_account_id, '-', '')
              AND NOT EXISTS (
                  SELECT 1
                  FROM resumes AS resume
                  WHERE resume.profile_id = profile.id
                    AND resume.active IS TRUE
                    AND resume.verified IS TRUE
              )
            """
        ).bindparams(bootstrap_account_id=_BOOTSTRAP_ADMIN_ACCOUNT_ID)
    )


def downgrade() -> None:
    # Do not reactivate profiles automatically: a profile demoted for missing a
    # verified resume is not safe to put back into background processing.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE match_evaluations RESET "
            "(autovacuum_analyze_scale_factor, autovacuum_analyze_threshold)"
        )
    op.drop_index("ix_match_evaluations_profile_created", table_name="match_evaluations")
    op.drop_index("ix_applications_profile_match_evaluation", table_name="applications")
