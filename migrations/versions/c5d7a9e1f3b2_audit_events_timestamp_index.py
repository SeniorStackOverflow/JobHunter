"""Index audit events by time for the newest-first panel and API queries.

Revision ID: c5d7a9e1f3b2
Revises: b7f1e4c9a2d3

Without it the overview scanned and sorted the whole audit log (hundreds of
thousands of rows) on every render to show the six latest events.
"""

from alembic import op

revision = "c5d7a9e1f3b2"
down_revision = "b7f1e4c9a2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(op.f("ix_audit_events_timestamp"), "audit_events", ["timestamp"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_events_timestamp"), table_name="audit_events")
