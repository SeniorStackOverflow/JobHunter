"""Store category choices per (profile, source).

Revision ID: d8e2f4a6b1c3
Revises: c5d7a9e1f3b2

Every adapter has its own category vocabulary, so the three profile-wide
category lists cannot describe several sources. The existing lists are copied
unchanged to every source of each profile: behaviour after the migration is the
same until the owner changes a source's categories.
"""

import sqlalchemy as sa
from alembic import op

revision = "d8e2f4a6b1c3"
down_revision = "c5d7a9e1f3b2"
branch_labels = None
depends_on = None

_LISTS = ("search_categories", "auto_send_categories", "excluded_categories")


def upgrade() -> None:
    op.add_column(
        "profile_source_preferences",
        sa.Column(
            "categories_configured", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    for name in _LISTS:
        op.add_column(
            "profile_source_preferences",
            sa.Column(name, sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        )

    connection = op.get_bind()
    preferences = sa.table(
        "job_preferences",
        sa.column("profile_id", sa.Uuid()),
        sa.column("allowed_categories", sa.JSON()),
        sa.column("auto_send_categories", sa.JSON()),
        sa.column("forbidden_categories", sa.JSON()),
    )
    sources = sa.table("job_sources", sa.column("id", sa.Uuid()))
    choices = sa.table(
        "profile_source_preferences",
        sa.column("profile_id", sa.Uuid()),
        sa.column("source_id", sa.Uuid()),
        sa.column("enabled", sa.Boolean()),
        sa.column("categories_configured", sa.Boolean()),
        sa.column("search_categories", sa.JSON()),
        sa.column("auto_send_categories", sa.JSON()),
        sa.column("excluded_categories", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    source_ids = [row.id for row in connection.execute(sa.select(sources.c.id))]
    existing = {
        (row.profile_id, row.source_id)
        for row in connection.execute(sa.select(choices.c.profile_id, choices.c.source_id))
    }
    for row in connection.execute(sa.select(preferences)):
        values = {
            "search_categories": list(row.allowed_categories or []),
            "auto_send_categories": list(row.auto_send_categories or []),
            "excluded_categories": list(row.forbidden_categories or []),
        }
        if not any(values.values()):
            continue
        for source_id in source_ids:
            if (row.profile_id, source_id) in existing:
                connection.execute(
                    sa.update(choices)
                    .where(
                        choices.c.profile_id == row.profile_id,
                        choices.c.source_id == source_id,
                    )
                    .values(categories_configured=True, **values)
                )
            else:
                connection.execute(
                    sa.insert(choices).values(
                        profile_id=row.profile_id,
                        source_id=source_id,
                        enabled=True,
                        categories_configured=True,
                        created_at=sa.func.now(),
                        updated_at=sa.func.now(),
                        **values,
                    )
                )


def downgrade() -> None:
    # Rows created only to hold a category choice mean "source selected", which is
    # also what a missing row means, so they can stay.
    for name in (*reversed(_LISTS), "categories_configured"):
        op.drop_column("profile_source_preferences", name)
