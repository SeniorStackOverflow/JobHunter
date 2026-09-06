from __future__ import annotations

# FastAPI's declarative dependency/form parameters intentionally call Depends/Form.
# ruff: noqa: B008
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import routes as _admin_routes
from app.admin.routes import require_admin, require_csrf
from app.database import get_session
from app.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])


async def phone_health_context(session: AsyncSession) -> dict[str, Any]:
    """Query phone channel health and aggregate component status."""
    from app.models.entities import PhoneChannelHealth, PhoneDeviceSnapshot
    from app.models.enums import PhoneComponentStatus
    from app.phone.health import HealthComponent, agent_component_is_stale, channel_status

    rows = list((await session.scalars(select(PhoneChannelHealth))).all())
    agent_row = next((r for r in rows if r.component == "agent"), None)
    agent_stale = agent_row is not None and agent_component_is_stale(
        agent_row.updated_at,
        stale_after_seconds=get_settings().phone_health_stale_after_seconds,
    )

    def _effective_status(row: PhoneChannelHealth) -> PhoneComponentStatus:
        if row.component == "agent" and agent_stale:
            return PhoneComponentStatus.UNAVAILABLE
        return row.status

    components = [
        HealthComponent(r.component, _effective_status(r), r.detail, r.last_ok_at) for r in rows
    ]

    # Read device snapshot
    device_snapshot = await session.scalar(
        select(PhoneDeviceSnapshot).where(PhoneDeviceSnapshot.id == "current")
    )

    # Read auto-answer state and active call from Redis
    from redis.exceptions import RedisError

    from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY, CALL_OWNED_KEY

    redis = None
    stopped = False
    owned = None
    try:
        redis = _admin_routes._phone_redis()
        stopped = (await redis.get(AUTO_ANSWER_STOPPED_KEY)) == "1"
        owned = await redis.get(CALL_OWNED_KEY)
    except (OSError, RedisError) as exc:
        logger.warning("phone_health_redis_unreachable", error=type(exc).__name__)
    finally:
        if redis is not None:
            await redis.aclose()

    active_call = None
    if owned:
        from app.models.entities import CommunicationSession

        try:
            call = await session.get(CommunicationSession, UUID(owned))
        except ValueError:
            call = None
        if call is not None and call.ended_at is None:
            active_call = {"session_id": owned, "script_stage": call.script_stage}

    return {
        "channel": channel_status(components).value if components else "unknown",
        "components": [
            {
                "component": c.component,
                "status": c.status.value,
                "detail": c.detail,
                "last_ok_at": c.last_ok_at,
            }
            for c in sorted(components, key=lambda c: c.component)
        ],
        "configured": get_settings().phone_agent_enabled,
        # ``updated_at`` is the snapshot row's own column, not part of the stored
        # payload; the template renders it with ``format_dt`` so it stays a datetime.
        "device": (
            {**device_snapshot.payload, "updated_at": device_snapshot.updated_at}
            if device_snapshot
            else {}
        ),
        "auto_answer": {
            "enabled": get_settings().phone_auto_answer_enabled,
            "stopped": stopped,
        },
        "active_call": active_call,
    }


@router.post("/admin/phone/auto-answer/{action}")
async def phone_auto_answer_toggle(
    action: str,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    if action not in {"stop", "resume"}:
        raise HTTPException(status_code=404)
    from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY

    redis = _admin_routes._phone_redis()
    try:
        if action == "stop":
            await redis.set(AUTO_ANSWER_STOPPED_KEY, "1")
        else:
            await redis.delete(AUTO_ANSWER_STOPPED_KEY)
    finally:
        await redis.aclose()
    await _admin_routes._audit_admin(
        session, f"phone.auto_answer.{action}", "phone_channel", "auto_answer"
    )
    await session.commit()
    return RedirectResponse("/?view=diagnostics", status_code=303)


@router.post("/admin/phone/call/{session_id}/{action}")
async def phone_call_action(
    session_id: UUID,
    action: str,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    if action not in {"hangup", "mute"}:
        raise HTTPException(status_code=404)
    from app.phone.orchestrator import CALL_CMD_KEY, CALL_OWNED_KEY

    redis = _admin_routes._phone_redis()
    try:
        owned = await redis.get(CALL_OWNED_KEY)
        if owned != str(session_id):
            raise HTTPException(status_code=409, detail="not the active call")
        await redis.set(CALL_CMD_KEY, f"{action}:{session_id}", ex=60)
    finally:
        await redis.aclose()
    await _admin_routes._audit_admin(
        session, f"phone.call.{action}", "communication_session", str(session_id)
    )
    await session.commit()
    return RedirectResponse("/?view=diagnostics", status_code=303)
