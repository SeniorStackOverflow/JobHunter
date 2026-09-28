"""add account ownership and invite foundation

Revision ID: c8a4d1f9e210
Revises: b7e3c9a1d4f2
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "c8a4d1f9e210"
down_revision: str | None = "b7e3c9a1d4f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BOOTSTRAP_ADMIN_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("role", sa.String(length=5), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("invite_allowance", sa.Integer(), nullable=False),
        sa.Column("allow_open_invites", sa.Boolean(), nullable=False),
        sa.Column("max_profiles", sa.Integer(), nullable=False),
        sa.Column("allow_phone", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("invite_allowance >= 0", name="accounts_invite_allowance_nonnegative"),
        sa.CheckConstraint("max_profiles >= 1", name="accounts_max_profiles_positive"),
        sa.PrimaryKeyConstraint("id"),
    )
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Uuid()),
        sa.column("role", sa.String()),
        sa.column("status", sa.String()),
        sa.column("session_version", sa.Integer()),
        sa.column("invite_allowance", sa.Integer()),
        sa.column("allow_open_invites", sa.Boolean()),
        sa.column("max_profiles", sa.Integer()),
        sa.column("allow_phone", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    op.bulk_insert(
        accounts,
        [
            {
                "id": BOOTSTRAP_ADMIN_ACCOUNT_ID,
                "role": "admin",
                "status": "active",
                "session_version": 0,
                "invite_allowance": 0,
                "allow_open_invites": True,
                "max_profiles": 100,
                "allow_phone": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    with op.batch_alter_table("user_profiles") as batch_op:
        batch_op.add_column(sa.Column("owner_account_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(length=8),
                nullable=False,
                server_default="active",
            )
        )
    profiles = sa.table(
        "user_profiles",
        sa.column("owner_account_id", sa.Uuid()),
    )
    op.get_bind().execute(
        sa.update(profiles).values(owner_account_id=BOOTSTRAP_ADMIN_ACCOUNT_ID)
    )
    with op.batch_alter_table("user_profiles") as batch_op:
        batch_op.alter_column("owner_account_id", nullable=False)
        batch_op.alter_column("status", server_default=None)
        batch_op.create_foreign_key(
            "fk_user_profiles_owner_account_id_accounts",
            "accounts",
            ["owner_account_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_user_profiles_owner_account_id", ["owner_account_id"], unique=False
        )

    op.create_table(
        "account_identities",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=6), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "subject", name="uq_account_identity_provider_subject"
        ),
        sa.UniqueConstraint(
            "account_id", "provider", name="uq_account_identity_account_provider"
        ),
    )
    op.create_index(
        "ix_account_identities_account_id",
        "account_identities",
        ["account_id"],
        unique=False,
    )

    op.create_table(
        "invites",
        sa.Column("created_by_account_id", sa.Uuid(), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("target_email", sa.String(length=320), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_by_account_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(redeemed_at IS NULL AND redeemed_by_account_id IS NULL) OR "
            "(redeemed_at IS NOT NULL AND redeemed_by_account_id IS NOT NULL)",
            name="invites_redemption_pair",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_account_id"], ["accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["redeemed_by_account_id"], ["accounts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_invites_created_by_account_id",
        "invites",
        ["created_by_account_id"],
        unique=False,
    )
    op.create_index("ix_invites_expires_at", "invites", ["expires_at"], unique=False)
    op.create_index(
        "ix_invites_redeemed_by_account_id",
        "invites",
        ["redeemed_by_account_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_invites_redeemed_by_account_id", table_name="invites")
    op.drop_index("ix_invites_expires_at", table_name="invites")
    op.drop_index("ix_invites_created_by_account_id", table_name="invites")
    op.drop_table("invites")
    op.drop_index("ix_account_identities_account_id", table_name="account_identities")
    op.drop_table("account_identities")
    with op.batch_alter_table("user_profiles") as batch_op:
        batch_op.drop_index("ix_user_profiles_owner_account_id")
        batch_op.drop_constraint(
            "fk_user_profiles_owner_account_id_accounts", type_="foreignkey"
        )
        batch_op.drop_column("status")
        batch_op.drop_column("owner_account_id")
    op.drop_table("accounts")
