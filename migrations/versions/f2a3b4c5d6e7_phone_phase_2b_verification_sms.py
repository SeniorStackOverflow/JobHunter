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


def upgrade() -> None:
    bind = op.get_bind()
    # This check intentionally happens before any schema mutation. Existing facts
    # are preserved for an operator to resolve instead of selecting a winner.
    _abort_for_duplicate_facts(bind)

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
                    name="fk_communication_sessions_related_session_id_communication_sessions",
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

    bind = op.get_bind()
    # transport_external_id did not exist in the parent revision, so all values
    # are necessarily NULL at this point. Keep this guard next to the constraint
    # creation to protect future schema variants that may already contain values.
    duplicate_transport = (
        bind.execute(
            sa.text(
                """
            SELECT transport, channel, transport_external_id, COUNT(*) AS row_count
            FROM communication_sessions
            WHERE transport_external_id IS NOT NULL
            GROUP BY transport, channel, transport_external_id
            HAVING COUNT(*) > 1
            ORDER BY transport, channel, transport_external_id
            """
            )
        )
        .mappings()
        .all()
    )
    if duplicate_transport:
        raise RuntimeError(
            "Cannot add uq_communication_sessions_transport_channel_external_id: "
            "duplicate session transport IDs exist for (transport, channel, "
            "transport_external_id); resolve these rows before retrying the migration. "
            f"Duplicates: {[dict(row) for row in duplicate_transport]}"
        )

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


def downgrade() -> None:
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

    # ``processing`` is a transient state and is not part of the parent
    # revision's enum. Normalize it before restoring that constraint.
    op.execute(
        sa.text(
            "UPDATE communication_sessions SET summary_state = 'pending' "
            "WHERE summary_state = 'processing'"
        )
    )
    # SMS sessions have no PhoneGate event cursor. The parent schema requires
    # this legacy column, so use its neutral sentinel while downgrading.
    op.execute(
        sa.text(
            "UPDATE communication_sessions SET phonegate_event_id_start = 0 "
            "WHERE phonegate_event_id_start IS NULL"
        )
    )
    copy_from = _communication_sessions_without_summary_check(op.get_bind())
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
