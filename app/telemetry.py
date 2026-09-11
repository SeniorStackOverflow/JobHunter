from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.models.entities import ExternalCallEvent

_ALLOWED_OUTCOMES = {"success", "error", "timeout", "cancelled"}


def _as_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _as_int(value: Any, *, minimum: int = 0) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= minimum else None


def _event_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return utcnow()
    else:
        return utcnow()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_external_attempt(raw: dict[str, Any], *, attempt_no: int) -> dict[str, Any]:
    outcome = _as_text(raw.get("outcome"), 32) or "error"
    if outcome not in _ALLOWED_OUTCOMES:
        outcome = "error"
    http_status = _as_int(raw.get("http_status"), minimum=100)
    if http_status is not None and http_status > 999:
        http_status = None
    return {
        "occurred_at": _event_time(raw.get("occurred_at")),
        "provider": _as_text(raw.get("provider"), 64),
        "resource": _as_text(raw.get("model") or raw.get("resource"), 255),
        "attempt_no": attempt_no,
        "outcome": outcome,
        "http_status": http_status,
        "provider_error_code": _as_text(raw.get("provider_error_code"), 128),
        "exception_type": _as_text(raw.get("exception_type"), 128),
        "retryable": bool(raw.get("retryable", False)),
        "retry_after_seconds": _as_int(raw.get("retry_after_seconds"), minimum=0),
        "latency_ms": _as_int(raw.get("latency_ms"), minimum=0),
    }


async def record_external_call_attempts(
    session: AsyncSession,
    *,
    attempts: list[dict[str, Any]],
    subsystem: str,
    operation: str,
    upstream_service: str,
    logical_request_id: str,
    correlation_id: str | None = None,
    entity_type: str | None = None,
    entity_id: UUID | str | None = None,
    recovered: bool = False,
    metadata: dict[str, Any] | None = None,
) -> list[ExternalCallEvent]:
    rows: list[ExternalCallEvent] = []
    total = len(attempts)
    for index, raw in enumerate(attempts, start=1):
        normalized = normalize_external_attempt(raw, attempt_no=index)
        row = ExternalCallEvent(
            subsystem=subsystem[:64],
            operation=operation[:128],
            upstream_service=upstream_service[:64],
            provider=normalized["provider"],
            resource=normalized["resource"],
            logical_request_id=logical_request_id[:128],
            correlation_id=(correlation_id or logical_request_id)[:255],
            entity_type=entity_type[:64] if entity_type else None,
            entity_id=str(entity_id)[:255] if entity_id is not None else None,
            attempt_no=index,
            outcome=normalized["outcome"],
            http_status=normalized["http_status"],
            provider_error_code=normalized["provider_error_code"],
            exception_type=normalized["exception_type"],
            retryable=normalized["retryable"],
            retry_after_seconds=normalized["retry_after_seconds"],
            latency_ms=normalized["latency_ms"],
            recovered=recovered,
            is_final_attempt=index == total,
            event_metadata=dict(metadata or {}),
            occurred_at=normalized["occurred_at"],
        )
        session.add(row)
        rows.append(row)
    if rows:
        await session.flush()
    return rows


def _http_class(status: int) -> str:
    return f"{status // 100}xx"


async def external_call_metrics(
    session: AsyncSession, start: datetime, end: datetime
) -> dict[str, Any]:
    rows = list(
        (
            await session.scalars(
                select(ExternalCallEvent)
                .where(
                    ExternalCallEvent.occurred_at >= start,
                    ExternalCallEvent.occurred_at < end,
                )
                .order_by(ExternalCallEvent.occurred_at, ExternalCallEvent.id)
            )
        ).all()
    )
    failures = [row for row in rows if row.outcome != "success"]
    logical_requests = {row.logical_request_id for row in rows}
    affected_requests = {row.logical_request_id for row in failures}
    recovered_requests = {
        row.logical_request_id for row in rows if row.recovered and row.logical_request_id
    }
    final_failed_requests = {
        row.logical_request_id
        for row in rows
        if row.is_final_attempt and row.outcome != "success" and not row.recovered
    }
    http_status = Counter(str(row.http_status) for row in rows if row.http_status is not None)
    http_errors = Counter(
        str(row.http_status)
        for row in failures
        if row.http_status is not None and row.http_status >= 400
    )
    http_error_classes = Counter(
        _http_class(row.http_status)
        for row in failures
        if row.http_status is not None and row.http_status >= 400
    )
    exceptions = Counter(row.exception_type for row in failures if row.exception_type)
    provider_errors = Counter((row.provider or "unknown") for row in failures)
    subsystem_errors = Counter(row.subsystem for row in failures)
    return {
        "total_attempts": len(rows),
        "successful_attempts": len(rows) - len(failures),
        "failed_attempts": len(failures),
        "logical_requests": len(logical_requests),
        "affected_requests": len(affected_requests),
        "recovered_requests": len(recovered_requests),
        "final_failed_requests": len(final_failed_requests),
        "by_http_status": dict(sorted(http_status.items(), key=lambda item: int(item[0]))),
        "http_errors_by_status": dict(sorted(http_errors.items(), key=lambda item: int(item[0]))),
        "http_errors_by_class": dict(sorted(http_error_classes.items())),
        "by_exception_type": dict(sorted(exceptions.items())),
        "errors_by_provider": dict(sorted(provider_errors.items())),
        "errors_by_subsystem": dict(sorted(subsystem_errors.items())),
    }
