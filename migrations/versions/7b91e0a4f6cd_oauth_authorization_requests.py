"""add one-time OAuth authorization requests

Revision ID: 7b91e0a4f6cd
Revises: f4385e61a75c
Create Date: 2026-08-03 16:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "7b91e0a4f6cd"
down_revision: str | None = "f4385e61a75c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_authorization_requests",
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("binding_hash", sa.String(length=64), nullable=False),
        sa.Column("encrypted_code_verifier", sa.LargeBinary(), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_authorization_requests")),
        sa.UniqueConstraint(
            "state_hash",
            name=op.f("uq_oauth_authorization_requests_state_hash"),
        ),
    )
    op.create_index(
        "ix_oauth_authorization_requests_expires_at",
        "oauth_authorization_requests",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_oauth_authorization_requests_expires_at",
        table_name="oauth_authorization_requests",
    )
    op.drop_table("oauth_authorization_requests")
