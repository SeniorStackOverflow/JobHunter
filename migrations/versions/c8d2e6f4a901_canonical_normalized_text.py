"""Preserve complete normalized vacancy fields, including multi-city locations.

Revision ID: c8d2e6f4a901
Revises: b3f6c9e2d5a8
"""

import sqlalchemy as sa
from alembic import op

revision = "c8d2e6f4a901"
down_revision = "b3f6c9e2d5a8"
branch_labels = None
depends_on = None

FIELDS = ("normalized_company", "normalized_title", "normalized_location")


def upgrade() -> None:
    with op.batch_alter_table("canonical_jobs") as batch:
        for name in FIELDS:
            batch.alter_column(
                name, existing_type=sa.String(255), type_=sa.Text(), existing_nullable=False
            )


def downgrade() -> None:
    # Refuse to discard real vacancy data while reverting the storage limit.
    too_long = op.get_bind().scalar(
        sa.text(
            "SELECT 1 FROM canonical_jobs WHERE length(normalized_company) > 255 "
            "OR length(normalized_title) > 255 OR length(normalized_location) > 255 LIMIT 1"
        )
    )
    if too_long:
        raise RuntimeError("Cannot shrink canonical fields without losing stored vacancy data")
    with op.batch_alter_table("canonical_jobs") as batch:
        for name in FIELDS:
            batch.alter_column(
                name, existing_type=sa.Text(), type_=sa.String(255), existing_nullable=False
            )
