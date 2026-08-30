"""Add machine-readable Gmail OAuth delivery errors.

Revision ID: a4f0c2d8e731
Revises: f1a2b3c4d5e6
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "a4f0c2d8e731"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "email_deliveries",
        sa.Column("error_code", sa.String(length=128), nullable=True),
    )
    op.create_index(
        op.f("ix_email_deliveries_error_code"),
        "email_deliveries",
        ["error_code"],
        unique=False,
    )
    op.execute(
        """
        UPDATE email_deliveries
           SET error_code = 'gmail_reauthorization_required'
         WHERE error_code IS NULL
           AND error = 'Gmail OAuth refresh failed; reauthorization is required'
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_email_deliveries_error_code"), table_name="email_deliveries")
    op.drop_column("email_deliveries", "error_code")
