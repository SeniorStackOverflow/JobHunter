"""store explicit review feedback for personal learning

Revision ID: c7d4e6f8a912
Revises: e8b7a1c42f10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d4e6f8a912"
down_revision: str | None = "e8b7a1c42f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_feedback_events",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("match_evaluation_id", sa.Uuid(), nullable=True),
        sa.Column("canonical_job_id", sa.Uuid(), nullable=False),
        sa.Column("source_job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum("approved", "rejected", name="reviewoutcome", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "reason_code",
            sa.Enum(
                "role",
                "salary",
                "schedule",
                "location",
                "company",
                "requirements",
                "vacancy_problem",
                "other",
                name="reviewreason",
                native_enum=False,
            ),
            nullable=True,
        ),
        sa.Column("reason_text", sa.String(length=500), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("learning_eligible", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", sa.String(length=128), nullable=True),
        sa.Column("source_content_hash", sa.String(length=64), nullable=True),
        sa.Column("profile_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("preference_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("resume_sha256", sa.String(length=64), nullable=True),
        sa.Column("feature_schema_version", sa.String(length=32), nullable=False),
        sa.Column("feature_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["canonical_job_id"], ["canonical_jobs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["match_evaluation_id"], ["match_evaluations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["user_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_job_id"], ["source_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id", name="uq_review_feedback_application"),
    )
    op.create_index(
        "ix_review_feedback_profile_created",
        "review_feedback_events",
        ["profile_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "review_learning_settings",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("influence_enabled", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["user_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_id", name="uq_review_learning_profile"),
    )


def downgrade() -> None:
    op.drop_table("review_learning_settings")
    op.drop_index("ix_review_feedback_profile_created", table_name="review_feedback_events")
    op.drop_table("review_feedback_events")
