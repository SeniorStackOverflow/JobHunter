"""Record the application-level LLM outcome on each match evaluation.

Revision ID: b7f1e4c9a2d3
Revises: a3e9c2d7b4f1

Historical evaluations keep NULL: their logical request was never stored and a
time-nearest correlation must not be presented as a foreign key.
"""

import sqlalchemy as sa
from alembic import op

revision = "b7f1e4c9a2d3"
down_revision = "a3e9c2d7b4f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "match_evaluations",
        sa.Column("llm_logical_request_id", sa.String(length=128), nullable=True),
    )
    op.add_column("match_evaluations", sa.Column("llm_outcome", sa.String(length=32), nullable=True))
    op.add_column(
        "match_evaluations", sa.Column("llm_failure_code", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "match_evaluations", sa.Column("llm_failure_path", sa.String(length=255), nullable=True)
    )
    op.add_column("match_evaluations", sa.Column("llm_attempts", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_match_evaluations_llm_logical_request_id"),
        "match_evaluations",
        ["llm_logical_request_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_match_evaluations_llm_logical_request_id"), table_name="match_evaluations"
    )
    op.drop_column("match_evaluations", "llm_attempts")
    op.drop_column("match_evaluations", "llm_failure_path")
    op.drop_column("match_evaluations", "llm_failure_code")
    op.drop_column("match_evaluations", "llm_outcome")
    op.drop_column("match_evaluations", "llm_logical_request_id")
