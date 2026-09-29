"""scope Gmail OAuth and mailbox state by account

Revision ID: d4b9a7c2e611
Revises: c8a4d1f9e210
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "d4b9a7c2e611"
down_revision: str | None = "c8a4d1f9e210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BOOTSTRAP_ADMIN_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")


def _backfill_account_id(table_name: str) -> None:
    table = sa.table(table_name, sa.column("account_id", sa.Uuid()))
    op.get_bind().execute(
        sa.update(table)
        .where(table.c.account_id.is_(None))
        .values(account_id=BOOTSTRAP_ADMIN_ACCOUNT_ID)
    )


def upgrade() -> None:
    with op.batch_alter_table("oauth_credentials") as batch_op:
        batch_op.add_column(sa.Column("account_id", sa.Uuid(), nullable=True))
    _backfill_account_id("oauth_credentials")
    with op.batch_alter_table("oauth_credentials") as batch_op:
        batch_op.alter_column("account_id", nullable=False)
        batch_op.create_foreign_key(
            "fk_oauth_credentials_account_id_accounts",
            "accounts",
            ["account_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_oauth_credentials_account_id", ["account_id"], unique=False
        )
        batch_op.drop_constraint("uq_oauth_credentials_provider", type_="unique")
        batch_op.create_unique_constraint(
            "uq_oauth_credentials_account_provider",
            ["account_id", "provider"],
        )

    with op.batch_alter_table("oauth_authorization_requests") as batch_op:
        batch_op.add_column(sa.Column("account_id", sa.Uuid(), nullable=True))
    _backfill_account_id("oauth_authorization_requests")
    with op.batch_alter_table("oauth_authorization_requests") as batch_op:
        batch_op.alter_column("account_id", nullable=False)
        batch_op.create_foreign_key(
            "fk_oauth_authorization_requests_account_id_accounts",
            "accounts",
            ["account_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_oauth_authorization_requests_account_id",
            ["account_id"],
            unique=False,
        )

    with op.batch_alter_table("email_delivery_events") as batch_op:
        batch_op.add_column(sa.Column("account_id", sa.Uuid(), nullable=True))
    _backfill_account_id("email_delivery_events")
    with op.batch_alter_table("email_delivery_events") as batch_op:
        batch_op.alter_column("account_id", nullable=False)
        batch_op.create_foreign_key(
            "fk_email_delivery_events_account_id_accounts",
            "accounts",
            ["account_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_email_delivery_events_account_id", ["account_id"], unique=False
        )
        batch_op.drop_constraint(
            "uq_email_delivery_event_provider_message",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_email_delivery_event_account_provider_message",
            ["account_id", "provider", "provider_message_id"],
        )

    op.create_table(
        "email_mailbox_cursors_v2",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("history_id", sa.String(length=64), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id", "provider"),
    )
    op.execute(
        sa.text(
            "INSERT INTO email_mailbox_cursors_v2 "
            "(account_id, provider, history_id, last_checked_at, updated_at) "
            "SELECT :account_id, provider, history_id, last_checked_at, updated_at "
            "FROM email_mailbox_cursors"
        ).bindparams(
            sa.bindparam(
                "account_id",
                value=BOOTSTRAP_ADMIN_ACCOUNT_ID,
                type_=sa.Uuid(),
            )
        )
    )
    op.drop_table("email_mailbox_cursors")
    op.rename_table("email_mailbox_cursors_v2", "email_mailbox_cursors")


def downgrade() -> None:
    # The old schema can represent only one Gmail mailbox. Rollback therefore
    # intentionally keeps only bootstrap-admin mailbox state/credentials.
    op.execute(
        sa.text(
            "DELETE FROM oauth_credentials WHERE account_id != :account_id"
        ).bindparams(
            sa.bindparam(
                "account_id",
                value=BOOTSTRAP_ADMIN_ACCOUNT_ID,
                type_=sa.Uuid(),
            )
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM oauth_authorization_requests WHERE account_id != :account_id"
        ).bindparams(
            sa.bindparam(
                "account_id",
                value=BOOTSTRAP_ADMIN_ACCOUNT_ID,
                type_=sa.Uuid(),
            )
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM email_delivery_events WHERE account_id != :account_id"
        ).bindparams(
            sa.bindparam(
                "account_id",
                value=BOOTSTRAP_ADMIN_ACCOUNT_ID,
                type_=sa.Uuid(),
            )
        )
    )

    op.create_table(
        "email_mailbox_cursors_v1",
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("history_id", sa.String(length=64), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider"),
    )
    op.execute(
        sa.text(
            "INSERT INTO email_mailbox_cursors_v1 "
            "(provider, history_id, last_checked_at, updated_at) "
            "SELECT provider, history_id, last_checked_at, updated_at "
            "FROM email_mailbox_cursors WHERE account_id = :account_id"
        ).bindparams(
            sa.bindparam(
                "account_id",
                value=BOOTSTRAP_ADMIN_ACCOUNT_ID,
                type_=sa.Uuid(),
            )
        )
    )
    op.drop_table("email_mailbox_cursors")
    op.rename_table("email_mailbox_cursors_v1", "email_mailbox_cursors")

    with op.batch_alter_table("email_delivery_events") as batch_op:
        batch_op.drop_constraint(
            "uq_email_delivery_event_account_provider_message",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_email_delivery_event_provider_message",
            ["provider", "provider_message_id"],
        )
        batch_op.drop_index("ix_email_delivery_events_account_id")
        batch_op.drop_constraint(
            "fk_email_delivery_events_account_id_accounts",
            type_="foreignkey",
        )
        batch_op.drop_column("account_id")

    with op.batch_alter_table("oauth_authorization_requests") as batch_op:
        batch_op.drop_index("ix_oauth_authorization_requests_account_id")
        batch_op.drop_constraint(
            "fk_oauth_authorization_requests_account_id_accounts",
            type_="foreignkey",
        )
        batch_op.drop_column("account_id")

    with op.batch_alter_table("oauth_credentials") as batch_op:
        batch_op.drop_constraint(
            "uq_oauth_credentials_account_provider",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_oauth_credentials_provider",
            ["provider"],
        )
        batch_op.drop_index("ix_oauth_credentials_account_id")
        batch_op.drop_constraint(
            "fk_oauth_credentials_account_id_accounts",
            type_="foreignkey",
        )
        batch_op.drop_column("account_id")
