"""Persist stable source catalog identities across code changes.

Revision ID: b3f6c9e2d5a8
Revises: f4a8c1d9e203
"""

from alembic import op
import sqlalchemy as sa

revision = "b3f6c9e2d5a8"
down_revision = "f4a8c1d9e203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_sources", sa.Column("catalog_key", sa.String(100), nullable=True))
    op.create_index("ix_job_sources_catalog_key", "job_sources", ["catalog_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_job_sources_catalog_key", table_name="job_sources")
    op.drop_column("job_sources", "catalog_key")
