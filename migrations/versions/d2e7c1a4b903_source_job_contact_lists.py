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
    with op.batch_alter_table("source_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("public_emails", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch_op.add_column(
            sa.Column("public_phones", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )

    json_array_function = (
        "json_array" if op.get_bind().dialect.name == "sqlite" else "json_build_array"
    )
    op.execute(
        sa.text(
            f"UPDATE source_jobs SET public_emails = {json_array_function}(public_email) "
            "WHERE public_email IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            f"UPDATE source_jobs SET public_phones = {json_array_function}(public_phone) "
            "WHERE public_phone IS NOT NULL"
        )
    )
    with op.batch_alter_table("source_jobs") as batch_op:
        batch_op.alter_column("public_emails", server_default=None)
        batch_op.alter_column("public_phones", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("source_jobs") as batch_op:
        batch_op.drop_column("public_phones")
        batch_op.drop_column("public_emails")
