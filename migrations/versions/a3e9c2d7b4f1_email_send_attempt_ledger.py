"""Add the daily hard-maximum ledger of provider submissions.

Revision ID: a3e9c2d7b4f1
Revises: d830a4b26f19

Historical rows are backfilled with one attempt per existing delivery. Only the
latest attempt is reconstructable from ``email_deliveries``: an accepted or later
bounced message counts on the local day of its recorded submission, an
in-flight/unknown one keeps its reservation, and a provider refusal before
acceptance does not consume the maximum.
"""

from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision = "a3e9c2d7b4f1"
down_revision = "d830a4b26f19"
branch_labels = None
depends_on = None

_LOCAL_TZ = ZoneInfo("Europe/Chisinau")
_ACCEPTED_OR_BOUNCED = {
    "provider_accepted",
    "delivered",
    "sent",
    "bounced_transient",
    "bounced_permanent",
    "recipient_rejected",
    "mailbox_full",
    "domain_rejected",
    "policy_rejected",
    "spam_rejected",
    "delivery_failed",
}


def _aware(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def upgrade() -> None:
    op.create_table(
        "email_send_attempts",
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("local_day", sa.String(length=10), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["email_deliveries.id"],
            name=op.f("fk_email_send_attempts_delivery_id_email_deliveries"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_email_send_attempts_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["user_profiles.id"],
            name=op.f("fk_email_send_attempts_profile_id_user_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_send_attempts")),
        sa.UniqueConstraint("delivery_id", "attempt_no", name="uq_email_send_attempt_delivery_no"),
    )
    op.create_index(
        "ix_email_send_attempts_profile_day",
        "email_send_attempts",
        ["profile_id", "local_day"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_send_attempts_application_id"),
        "email_send_attempts",
        ["application_id"],
        unique=False,
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT d.id, d.application_id, a.profile_id, d.status, d.attempt_count, "
            "d.submitted_at, d.provider_accepted_at, d.last_attempt_at, d.created_at "
            "FROM email_deliveries d JOIN applications a ON a.id = d.application_id"
        )
    ).all()
    attempts = sa.table(
        "email_send_attempts",
        sa.column("id", sa.Uuid()),
        sa.column("delivery_id", sa.Uuid()),
        sa.column("application_id", sa.Uuid()),
        sa.column("profile_id", sa.Uuid()),
        sa.column("attempt_no", sa.Integer()),
        sa.column("local_day", sa.String()),
        sa.column("outcome", sa.String()),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("finished_at", sa.DateTime(timezone=True)),
    )
    values = []
    for row in rows:
        status = str(row.status)
        submitted = _aware(row.submitted_at) or _aware(row.provider_accepted_at)
        attempted = _aware(row.last_attempt_at) or _aware(row.created_at)
        if submitted is not None or status in _ACCEPTED_OR_BOUNCED:
            outcome = "provider_accepted"
            started_at = submitted or attempted
        elif status == "delivery_unknown":
            outcome = "delivery_unknown"
            started_at = attempted
        elif status == "sending":
            outcome = "in_flight"
            started_at = attempted
        else:
            outcome = "not_transmitted"
            started_at = attempted
        if started_at is None:
            continue
        values.append(
            {
                "id": uuid4(),
                "delivery_id": _uuid(row.id),
                "application_id": _uuid(row.application_id),
                "profile_id": _uuid(row.profile_id),
                "attempt_no": max(1, int(row.attempt_count or 1)),
                "local_day": started_at.astimezone(_LOCAL_TZ).date().isoformat(),
                "outcome": outcome,
                "started_at": started_at,
                "finished_at": None if outcome == "in_flight" else started_at,
            }
        )
    if values:
        op.bulk_insert(attempts, values)


def _uuid(value: object) -> object:
    from uuid import UUID

    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def downgrade() -> None:
    op.drop_index(op.f("ix_email_send_attempts_application_id"), table_name="email_send_attempts")
    op.drop_index("ix_email_send_attempts_profile_day", table_name="email_send_attempts")
    op.drop_table("email_send_attempts")
