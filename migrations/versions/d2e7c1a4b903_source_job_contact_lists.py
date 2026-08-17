"""store every public email and phone found on a source job

Revision ID: d2e7c1a4b903
Revises: 9c4e7d2a81f0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2e7c1a4b903"
down_revision: str | None = "9c4e7d2a81f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_jobs",
        sa.Column("public_emails", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "source_jobs",
        sa.Column("public_phones", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.execute(
        "UPDATE source_jobs SET public_emails = json_build_array(public_email) "
        "WHERE public_email IS NOT NULL"
    )
    op.execute(
        "UPDATE source_jobs SET public_phones = json_build_array(public_phone) "
        "WHERE public_phone IS NOT NULL"
    )
    op.alter_column("source_jobs", "public_emails", server_default=None)
    op.alter_column("source_jobs", "public_phones", server_default=None)


def downgrade() -> None:
    op.drop_column("source_jobs", "public_phones")
    op.drop_column("source_jobs", "public_emails")
