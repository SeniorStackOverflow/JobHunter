"""separate matching-relevant source versions from crawler metadata

Revision ID: f1a2b3c4d5e6
Revises: c7d4e6f8a912
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import Enum
from typing import Any, cast

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "c7d4e6f8a912"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MATCHING_FIELDS = (
    "application_url",
    "category",
    "cities",
    "company",
    "currency",
    "description",
    "employer_url",
    "employment_type",
    "location",
    "no_experience",
    "public_email",
    "public_emails",
    "public_phone",
    "public_phones",
    "required_experience",
    "requirements",
    "responsibilities",
    "salary_max",
    "salary_min",
    "salary_text",
    "schedule",
    "status",
    "subcategory",
    "title",
    "workplace_type",
)
_MATCHING_FIELD_SET = frozenset(_MATCHING_FIELDS)


def _hash_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return sorted(value, key=lambda item: str(item))
    if isinstance(value, tuple):
        return sorted(value, key=lambda item: str(item))
    return value


def _matching_hash(row: Mapping[str, Any]) -> str:
    payload = {field: _hash_value(row.get(field)) for field in _MATCHING_FIELDS}
    serialized = json.dumps((payload,), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _requires_rematch(changed_fields: Any) -> bool:
    if not isinstance(changed_fields, (list, tuple, set, frozenset)):
        return True
    return any(str(field) in _MATCHING_FIELD_SET for field in changed_fields)


def upgrade() -> None:
    with op.batch_alter_table("source_jobs") as batch_op:
        batch_op.add_column(sa.Column("matching_content_hash", sa.String(length=64), nullable=True))
    with op.batch_alter_table("match_evaluations") as batch_op:
        batch_op.add_column(sa.Column("source_matching_hash", sa.String(length=64), nullable=True))
    with op.batch_alter_table("job_snapshots") as batch_op:
        batch_op.add_column(
            sa.Column(
                "requires_rematch",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )

    bind = op.get_bind()
    source_jobs = sa.table(
        "source_jobs",
        sa.column("id", sa.Uuid()),
        *(sa.column(field) for field in _MATCHING_FIELDS),
        sa.column("matching_content_hash", sa.String(length=64)),
    )
    source_updates = [
        {
            "row_id": row["id"],
            "matching_hash": _matching_hash(cast(Mapping[str, Any], row)),
        }
        for row in bind.execute(sa.select(source_jobs)).mappings()
    ]
    if source_updates:
        bind.execute(
            source_jobs.update()
            .where(source_jobs.c.id == sa.bindparam("row_id"))
            .values(matching_content_hash=sa.bindparam("matching_hash")),
            source_updates,
        )

    with op.batch_alter_table("source_jobs") as batch_op:
        batch_op.alter_column(
            "matching_content_hash",
            existing_type=sa.String(length=64),
            nullable=False,
        )

    snapshots = sa.table(
        "job_snapshots",
        sa.column("id", sa.Uuid()),
        sa.column("source_job_id", sa.Uuid()),
        sa.column("changed_fields", sa.JSON()),
        sa.column("requires_rematch", sa.Boolean()),
        sa.column("timestamp", sa.DateTime(timezone=True)),
    )
    snapshot_updates = [
        {
            "row_id": row["id"],
            "requires_rematch_value": _requires_rematch(row["changed_fields"]),
        }
        for row in bind.execute(sa.select(snapshots.c.id, snapshots.c.changed_fields)).mappings()
    ]
    if snapshot_updates:
        bind.execute(
            snapshots.update()
            .where(snapshots.c.id == sa.bindparam("row_id"))
            .values(requires_rematch=sa.bindparam("requires_rematch_value")),
            snapshot_updates,
        )

    evaluations = sa.table(
        "match_evaluations",
        sa.column("id", sa.Uuid()),
        sa.column("source_job_id", sa.Uuid()),
        sa.column("source_content_hash", sa.String(length=64)),
        sa.column("source_matching_hash", sa.String(length=64)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    source_hash = (
        sa.select(source_jobs.c.matching_content_hash)
        .where(source_jobs.c.id == evaluations.c.source_job_id)
        .scalar_subquery()
    )
    relevant_revision_exists = (
        sa.select(snapshots.c.id)
        .where(
            snapshots.c.source_job_id == evaluations.c.source_job_id,
            snapshots.c.requires_rematch.is_(True),
            snapshots.c.timestamp > evaluations.c.created_at,
        )
        .exists()
    )
    bind.execute(
        evaluations.update()
        .where(
            evaluations.c.source_content_hash.is_not(None),
            ~relevant_revision_exists,
        )
        .values(source_matching_hash=source_hash)
    )

    applications = sa.table(
        "applications",
        sa.column("id", sa.Uuid()),
        sa.column("match_evaluation_id", sa.Uuid()),
        sa.column("source_job_id", sa.Uuid()),
        sa.column("policy_result", sa.JSON()),
    )
    repair_rows = bind.execute(
        sa.select(applications.c.id, applications.c.policy_result)
        .join(
            evaluations,
            evaluations.c.id == applications.c.match_evaluation_id,
        )
        .join(source_jobs, source_jobs.c.id == applications.c.source_job_id)
        .where(
            evaluations.c.source_matching_hash.is_not(None),
            evaluations.c.source_matching_hash == source_jobs.c.matching_content_hash,
        )
    ).mappings()
    application_updates: list[dict[str, Any]] = []
    for row in repair_rows:
        policy_result = row["policy_result"]
        if not isinstance(policy_result, dict):
            continue
        if (
            policy_result.get("safe_stop_reason") != "match_evaluation_stale"
            and policy_result.get("requires_rematch") is not True
        ):
            continue
        repaired = dict(policy_result)
        repaired.pop("safe_stop_reason", None)
        repaired.pop("requires_rematch", None)
        raw_failed = repaired.get("rules_failed")
        if isinstance(raw_failed, list):
            repaired["rules_failed"] = [
                item for item in raw_failed if item != "match_evaluation_current"
            ]
        application_updates.append({"row_id": row["id"], "policy_result_value": repaired})
    if application_updates:
        bind.execute(
            applications.update()
            .where(applications.c.id == sa.bindparam("row_id"))
            .values(policy_result=sa.bindparam("policy_result_value")),
            application_updates,
        )

    op.create_index(
        "ix_job_snapshots_source_rematch_timestamp",
        "job_snapshots",
        ["source_job_id", "requires_rematch", "timestamp"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_job_snapshots_source_rematch_timestamp",
        table_name="job_snapshots",
    )
    with op.batch_alter_table("job_snapshots") as batch_op:
        batch_op.drop_column("requires_rematch")
    with op.batch_alter_table("match_evaluations") as batch_op:
        batch_op.drop_column("source_matching_hash")
    with op.batch_alter_table("source_jobs") as batch_op:
        batch_op.drop_column("matching_content_hash")
