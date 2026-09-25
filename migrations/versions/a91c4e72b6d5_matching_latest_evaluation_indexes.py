"""add latest-evaluation indexes"""
from __future__ import annotations
from collections.abc import Sequence
from alembic import op
revision: str = "a91c4e72b6d5"
down_revision: str | None = "ef3a1b2c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_index("ix_match_evaluations_profile_source_latest", "match_evaluations", ["profile_id", "source_job_id", "created_at", "id"])
    op.create_index("ix_match_evaluations_profile_canonical_latest", "match_evaluations", ["profile_id", "canonical_job_id", "created_at", "id"])

def downgrade() -> None:
    op.drop_index("ix_match_evaluations_profile_canonical_latest", table_name="match_evaluations")
    op.drop_index("ix_match_evaluations_profile_source_latest", table_name="match_evaluations")
