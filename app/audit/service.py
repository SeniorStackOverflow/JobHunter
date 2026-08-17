from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import AuditEvent

SENSITIVE_KEYS = re.compile(r"token|secret|password|authorization|resume_text|raw_mime", re.I)


def sanitize_details(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if SENSITIVE_KEYS.search(str(key))
            else sanitize_details(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_details(item, depth + 1) for item in value[:100]]
    if isinstance(value, str) and len(value) > 2000:
        return f"{value[:2000]}…[truncated]"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def record_audit_event(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    correlation_id: str,
    decision: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        decision=decision,
        sanitized_details=sanitize_details(details or {}),
        correlation_id=correlation_id,
    )
    session.add(event)
    await session.flush()
    return event
