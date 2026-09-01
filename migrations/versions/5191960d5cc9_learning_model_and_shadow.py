"""learning model and shadow

Revision ID: 5191960d5cc9
Revises: a4f0c2d8e731
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5191960d5cc9"
down_revision: str | None = "a4f0c2d8e731"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "learning_model_versions",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("segment_key", sa.String(length=64), nullable=False),
        sa.Column("feature_spec_version", sa.String(length=32), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("n_labels", sa.Integer(), nullable=False),
        sa.Column("n_approved", sa.Integer(), nullable=False),
        sa.Column("n_rejected", sa.Integer(), nullable=False),
        sa.Column("cv_auc", sa.Float(), nullable=False),
        sa.Column("cv_logloss", sa.Float(), nullable=False),
        sa.Column("cv_ece", sa.Float(), nullable=False),
        sa.Column("cv_ran", sa.Boolean(), nullable=False),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["user_profiles.id"],
            name=op.f("fk_learning_model_versions_profile_id_user_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_model_versions")),
        sa.UniqueConstraint(
            "profile_id",
            "segment_key",
            "trained_at",
            name="uq_learning_model_versions_identity",
        ),
    )
    op.create_index(
        op.f("ix_learning_model_versions_profile_id"),
        "learning_model_versions",
        ["profile_id"],
        unique=False,
    )
    op.create_table(
        "learning_shadow_outcomes",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("model_version_id", sa.Uuid(), nullable=True),
        sa.Column("segment_key", sa.String(length=64), nullable=False),
        sa.Column("p_approve", sa.Float(), nullable=False),
        sa.Column("ci_low", sa.Float(), nullable=False),
        sa.Column("ci_high", sa.Float(), nullable=False),
        sa.Column("support_ok", sa.Boolean(), nullable=False),
        sa.Column(
            "would_decide",
            sa.Enum("approve", "reject", "abstain", name="shadowdecision", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "human_decision",
            sa.Enum("approved", "rejected", name="reviewoutcome", native_enum=False),
            nullable=True,
        ),
        sa.Column(
            "human_reason",
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
        sa.Column("agreed", sa.Boolean(), nullable=True),
        sa.Column("sampled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_learning_shadow_outcomes_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["learning_model_versions.id"],
            name=op.f("fk_learning_shadow_outcomes_model_version_id_learning_model_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["user_profiles.id"],
            name=op.f("fk_learning_shadow_outcomes_profile_id_user_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_shadow_outcomes")),
        sa.UniqueConstraint(
            "application_id",
            "model_version_id",
            name="uq_learning_shadow_outcomes_identity",
        ),
    )
    op.create_index(
        op.f("ix_learning_shadow_outcomes_application_id"),
        "learning_shadow_outcomes",
        ["application_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_learning_shadow_outcomes_created_at"),
        "learning_shadow_outcomes",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_learning_shadow_outcomes_profile_id"),
        "learning_shadow_outcomes",
        ["profile_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_learning_shadow_outcomes_profile_id"),
        table_name="learning_shadow_outcomes",
    )
    op.drop_index(
        op.f("ix_learning_shadow_outcomes_created_at"),
        table_name="learning_shadow_outcomes",
    )
    op.drop_index(
        op.f("ix_learning_shadow_outcomes_application_id"),
        table_name="learning_shadow_outcomes",
    )
    op.drop_table("learning_shadow_outcomes")
    op.drop_index(
        op.f("ix_learning_model_versions_profile_id"),
        table_name="learning_model_versions",
    )
    op.drop_table("learning_model_versions")
