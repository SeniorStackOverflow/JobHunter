"""phone phase 2b: verification and SMS persistence

Revision ID: f2a3b4c5d6e7
Revises: e171bb9f241e
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e171bb9f241e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUMMARY_STATE = sa.Enum(
    "not_applicable",
    "pending",
    "processing",
    "done",
    "failed",
    "skipped",
    name="phonesummarystate",
    native_enum=False,
    create_constraint=True,
    constraint_name="ck_communication_sessions_summary_state",
)
_VERIFICATION_STATUS = sa.Enum(
    "not_applicable",
    "pending",
    "confirmed",
    "high_confidence",
    "needs_review",
    name="phoneverificationstatus",
    native_enum=False,
)
_CONFIRMATION_SOURCE = sa.Enum(
    "sms",
    "manual",
    name="callfactconfirmationsource",
    native_enum=False,
)
_SUMMARY_STATE_CHECK_SQL = (
    "summary_state IN ('not_applicable', 'pending', 'processing', 'done', 'failed', 'skipped')"
)
_LEGACY_SUMMARY_STATE_CHECK_SQL = (
    "summary_state IN ('not_applicable', 'pending', 'done', 'failed', 'skipped')"
)
_RELATED_SESSION_FK = "fk_communication_sessions_related_session"


def _find_duplicate_facts(bind: sa.Connection) -> list[dict[str, object]]:
    rows = bind.execute(
        sa.text(
            """
            SELECT session_id, field, COUNT(*) AS row_count
            FROM call_facts
            GROUP BY session_id, field
            HAVING COUNT(*) > 1
            ORDER BY session_id, field
            """
        )
    ).mappings()
    return [dict(row) for row in rows]


def _abort_for_duplicate_facts(bind: sa.Connection) -> None:
    duplicates = _find_duplicate_facts(bind)
    if duplicates:
        raise RuntimeError(
            "Cannot add uq_call_facts_session_field: duplicate CallFact rows exist "
            "for (session_id, field); resolve these rows before retrying the migration. "
            f"Duplicates: {duplicates}"
        )


def _communication_sessions_without_summary_check(bind: sa.Connection) -> sa.Table:
    table = sa.Table("communication_sessions", sa.MetaData(), autoload_with=bind)
    for constraint in list(table.constraints):
        if isinstance(constraint, sa.CheckConstraint) and "summary_state" in str(
            constraint.sqltext
        ):
            table.constraints.remove(constraint)
    return table


def _communication_sessions_with_legacy_summary_check(bind: sa.Connection) -> sa.Table:
    table = _communication_sessions_without_summary_check(bind)
    table.append_constraint(
        sa.CheckConstraint(
            _LEGACY_SUMMARY_STATE_CHECK_SQL,
            name="ck_communication_sessions_phonesummarystate",
        )
    )
    return table


def _find_duplicate_transport_groups(bind: sa.Connection) -> int:
    return int(
        bind.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT transport, channel, transport_external_id
                    FROM communication_sessions
                    WHERE transport_external_id IS NOT NULL
                    GROUP BY transport, channel, transport_external_id
                    HAVING COUNT(*) > 1
                ) AS duplicate_groups
                """
            )
        ).scalar_one()
    )


def _assert_no_duplicate_transport_ids(bind: sa.Connection) -> None:
    duplicate_groups = _find_duplicate_transport_groups(bind)
    if duplicate_groups:
        raise RuntimeError(
            "Cannot add uq_communication_sessions_transport_channel_external_id: "
            "duplicate session transport ID groups exist; resolve the rows before "
            "retrying the migration. Values are redacted from this diagnostic. "
            f"Duplicate groups: {duplicate_groups}"
        )


def _upgrade_sqlite() -> None:
    with op.batch_alter_table("communication_sessions", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column(
                "verification_status",
                _VERIFICATION_STATUS,
                nullable=False,
                server_default="not_applicable",
            )
        )
        batch_op.add_column(
            sa.Column("verification_revision", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("processing_started_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("transport_external_id", sa.String(length=96)))
        batch_op.add_column(
            sa.Column(
                "related_session_id",
                sa.Uuid(),
                sa.ForeignKey(
                    "communication_sessions.id",
                    name=_RELATED_SESSION_FK,
                    ondelete="SET NULL",
                ),
            )
        )
        batch_op.alter_column(
            "phonegate_event_id_start",
            existing_type=sa.Integer(),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.alter_column(
            "summary_state",
            existing_type=sa.String(length=14),
            type_=_SUMMARY_STATE,
            existing_nullable=False,
        )

    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.alter_column("verification_status", server_default=None)
        batch_op.alter_column("verification_revision", server_default=None)

    with op.batch_alter_table("call_facts") as batch_op:
        batch_op.add_column(sa.Column("confirmation_source", _CONFIRMATION_SOURCE))
        batch_op.add_column(sa.Column("confirmed_at", sa.DateTime(timezone=True)))

    _assert_no_duplicate_transport_ids(op.get_bind())
    op.create_index(
        "ix_communication_sessions_verification_status",
        "communication_sessions",
        ["verification_status"],
        unique=False,
    )
    op.create_index(
        "ix_communication_sessions_related_session_id",
        "communication_sessions",
        ["related_session_id"],
        unique=False,
    )
    with op.batch_alter_table("communication_sessions") as batch_op:
        batch_op.create_unique_constraint(
            "uq_communication_sessions_transport_channel_external_id",
            ["transport", "channel", "transport_external_id"],
        )
    with op.batch_alter_table("call_facts") as batch_op:
        batch_op.create_unique_constraint("uq_call_facts_session_field", ["session_id", "field"])


def _upgrade_postgresql() -> None:
    op.add_column(
        "communication_sessions",
        sa.Column(
            "verification_status",
            _VERIFICATION_STATUS,
            nullable=False,
            server_default="not_applicable",
        ),
    )
    op.add_column(
        "communication_sessions",
        sa.Column("verification_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "communication_sessions", sa.Column("processing_started_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "communication_sessions", sa.Column("transport_external_id", sa.String(length=96))
    )
    op.add_column(
        "communication_sessions",
        sa.Column(
            "related_session_id",
            sa.Uuid(),
            sa.ForeignKey(
                "communication_sessions.id",
                name=_RELATED_SESSION_FK,
                ondelete="SET NULL",
            ),
        ),
    )
    op.alter_column(
        "communication_sessions",
        "phonegate_event_id_start",
        existing_type=sa.Integer(),
        existing_nullable=False,
        nullable=True,
    )
    op.execute(
        sa.text(
            "ALTER TABLE communication_sessions ADD CONSTRAINT "
            "ck_communication_sessions_phonesummarystate CHECK ("
            f"{_SUMMARY_STATE_CHECK_SQL})"
        )
    )
    op.alter_column("communication_sessions", "verification_status", server_default=None)
    op.alter_column("communication_sessions", "verification_revision", server_default=None)
    op.add_column("call_facts", sa.Column("confirmation_source", _CONFIRMATION_SOURCE))
    op.add_column("call_facts", sa.Column("confirmed_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_communication_sessions_verification_status",
        "communication_sessions",
        ["verification_status"],
        unique=False,
    )
    op.create_index(
        "ix_communication_sessions_related_session_id",
        "communication_sessions",
        ["related_session_id"],
        unique=False,
    )
    _assert_no_duplicate_transport_ids(op.get_bind())
    op.create_unique_constraint(
        "uq_communication_sessions_transport_channel_external_id",
        "communication_sessions",
        ["transport", "channel", "transport_external_id"],
    )
    op.create_unique_constraint(
        "uq_call_facts_session_field", "call_facts", ["session_id", "field"]
    )


def upgrade() -> None:
    bind = op.get_bind()
    # This check intentionally happens before any schema mutation. Existing facts
    # are preserved for an operator to resolve instead of selecting a winner.
    _abort_for_duplicate_facts(bind)

    if bind.dialect.name == "sqlite":
        _upgrade_sqlite()
    else:
        _upgrade_postgresql()


def _reject_sms_downgrade(bind: sa.Connection) -> None:
    sms_rows = int(
        bind.execute(
            sa.text("SELECT COUNT(*) FROM communication_sessions WHERE channel = 'sms'")
        ).scalar_one()
    )
    if sms_rows:
        raise RuntimeError(
            "Cannot downgrade phone verification schema while SMS sessions exist; "
            "remove or migrate SMS sessions before retrying the downgrade."
        )


def _downgrade_sqlite() -> None:
    op.drop_index(
        "ix_communication_sessions_related_session_id", table_name="communication_sessions"
    )
    op.drop_index(
        "ix_communication_sessions_verification_status", table_name="communication_sessions"
    )
    with op.batch_alter_table("call_facts", recreate="always") as batch_op:
        batch_op.drop_constraint("uq_call_facts_session_field", type_="unique")
        batch_op.drop_column("confirmed_at")
        batch_op.drop_column("confirmation_source")
    op.execute(
        sa.text(
            "UPDATE communication_sessions SET summary_state = 'pending' "
            "WHERE summary_state = 'processing'"
        )
    )
    copy_from = _communication_sessions_with_legacy_summary_check(op.get_bind())
    with op.batch_alter_table(
        "communication_sessions", recreate="always", copy_from=copy_from
    ) as batch_op:
        batch_op.drop_constraint(
            "uq_communication_sessions_transport_channel_external_id", type_="unique"
        )
        batch_op.drop_column("related_session_id")
        batch_op.drop_column("transport_external_id")
        batch_op.drop_column("processing_started_at")
        batch_op.drop_column("verification_revision")
        batch_op.drop_column("verification_status")
        batch_op.alter_column(
            "phonegate_event_id_start",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
        )


def _downgrade_postgresql() -> None:
    op.drop_constraint("uq_call_facts_session_field", "call_facts", type_="unique")
    op.drop_constraint(
        "uq_communication_sessions_transport_channel_external_id",
        "communication_sessions",
        type_="unique",
    )
    op.drop_index(
        "ix_communication_sessions_related_session_id", table_name="communication_sessions"
    )
    op.drop_index(
        "ix_communication_sessions_verification_status", table_name="communication_sessions"
    )
    op.drop_column("call_facts", "confirmed_at")
    op.drop_column("call_facts", "confirmation_source")
    op.execute(
        sa.text(
            "UPDATE communication_sessions SET summary_state = 'pending' "
            "WHERE summary_state = 'processing'"
        )
    )
    op.drop_constraint(
        "ck_communication_sessions_phonesummarystate",
        "communication_sessions",
        type_="check",
    )
    op.execute(
        sa.text(
            "ALTER TABLE communication_sessions ADD CONSTRAINT "
            "ck_communication_sessions_phonesummarystate CHECK ("
            f"{_LEGACY_SUMMARY_STATE_CHECK_SQL})"
        )
    )
    op.drop_constraint(
        _RELATED_SESSION_FK,
        "communication_sessions",
        type_="foreignkey",
    )
    op.drop_column("communication_sessions", "related_session_id")
    op.drop_column("communication_sessions", "transport_external_id")
    op.drop_column("communication_sessions", "processing_started_at")
    op.drop_column("communication_sessions", "verification_revision")
    op.drop_column("communication_sessions", "verification_status")
    op.alter_column(
        "communication_sessions",
        "phonegate_event_id_start",
        existing_type=sa.Integer(),
        existing_nullable=True,
        nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    _reject_sms_downgrade(bind)
    if bind.dialect.name == "sqlite":
        _downgrade_sqlite()
    else:
        _downgrade_postgresql()
