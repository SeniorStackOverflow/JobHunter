"""Index bounded canonical and employer candidate searches.

Revision ID: f4a8c1d9e203
Revises: d8e2f4a6b1c3
"""

from alembic import op

revision = "f4a8c1d9e203"
down_revision = "d8e2f4a6b1c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_canonical_jobs_normalized_company", "canonical_jobs", ["normalized_company"]
    )
    op.create_index("ix_source_jobs_canonical_job_id", "source_jobs", ["canonical_job_id"])


def downgrade() -> None:
    op.drop_index("ix_source_jobs_canonical_job_id", table_name="source_jobs")
    op.drop_index("ix_canonical_jobs_normalized_company", table_name="canonical_jobs")
