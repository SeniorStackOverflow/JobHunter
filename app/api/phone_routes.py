from __future__ import annotations

# ruff: noqa: B008
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_actor
from app.database import get_session
from app.models.entities import (
    CommunicationSession,
    CommunicationTurn,
    PhoneChannelHealth,
    PhoneDeviceSnapshot,
)
from app.models.enums import PhoneComponentStatus
from app.phone.health import HealthComponent, agent_component_is_stale, channel_status
from app.phone.numbers import mask_phone
from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY
from app.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/phone", tags=["phone"])


@router.get("/status", dependencies=[Depends(require_api_actor)])
async def phone_status(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
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
    newest = await session.scalar(
        select(CommunicationSession).order_by(desc(CommunicationSession.started_at)).limit(1)
    )

    # Read device snapshot
    device_snapshot = await session.scalar(
        select(PhoneDeviceSnapshot).where(PhoneDeviceSnapshot.id == "current")
    )
    device_block = (
        {**device_snapshot.payload, "updated_at": device_snapshot.updated_at.isoformat()}
        if device_snapshot
        else {}
    )

    # Auto-answer state lives in Redis; the API process has no shared async-redis
    # dependency, so open a short-lived connection (this endpoint is diagnostic, not hot).
    redis = None
    try:
        redis = AsyncRedis.from_url(get_settings().redis_url, decode_responses=True)
        raw_stopped: str | None = await redis.get(AUTO_ANSWER_STOPPED_KEY)
        stopped = raw_stopped == "1"
    except (OSError, RedisError) as exc:
        logger.warning("phone_status_redis_unreachable", error=type(exc).__name__)
        stopped = False
    finally:
        if redis is not None:
            await redis.aclose()

    def _call_block(state: str, sess: CommunicationSession | None) -> dict[str, Any]:
        return {
            "state": state,
            "session_id": str(sess.id) if sess else None,
            "caller_number": mask_phone(sess.remote_address) if sess else None,
            "auto_answered": bool(sess.auto_answered) if sess else False,
            "script_stage": sess.script_stage if sess else None,
        }

    # Determine current_call state
    if newest is None or newest.ended_at is not None:
        current_call = _call_block("idle", None)
    elif newest.answered_at is not None:
        current_call = _call_block("connected", newest)
    else:
        current_call = _call_block("ringing", newest)

    return {
        "channel": channel_status(components).value if components else "unknown",
        "agent": {
            "last_ok_at": agent_row.last_ok_at.isoformat()
            if agent_row and agent_row.last_ok_at
            else None,
            "status": _effective_status(agent_row).value if agent_row else "unknown",
            "stale": agent_stale,
        },
        "components": [
            {
                "component": r.component,
                "status": _effective_status(r).value,
                "detail": r.detail,
                "last_ok_at": r.last_ok_at.isoformat() if r.last_ok_at else None,
            }
            for r in rows
        ],
        "device": device_block,
        "current_call": current_call,
        "auto_answer": {
            "enabled": get_settings().phone_auto_answer_enabled,
            "stopped": stopped,
            "last_decision": None,
        },
    }


def _session_row(call: CommunicationSession, turn_count: int) -> dict[str, Any]:
    return {
        "id": str(call.id),
        "profile_id": str(call.profile_id),
        "application_id": str(call.application_id) if call.application_id else None,
        "direction": call.direction.value,
        "remote_address": mask_phone(call.remote_address),
        "started_at": call.started_at.isoformat(),
        "answered_at": call.answered_at.isoformat() if call.answered_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "outcome": call.outcome.value if call.outcome else None,
        "needs_review": call.needs_review,
        "turn_count": turn_count,
    }


@router.get("/sessions", dependencies=[Depends(require_api_actor)])
async def list_sessions(
    limit: int = 50, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    calls = list(
        (
            await session.scalars(
                select(CommunicationSession)
                .order_by(desc(CommunicationSession.started_at))
                .limit(limit)
            )
        ).all()
    )
    counts: dict[UUID, int] = {
        session_id: int(count)
        for session_id, count in (
            await session.execute(
                select(CommunicationTurn.session_id, func.count(CommunicationTurn.id))
                .where(CommunicationTurn.session_id.in_([c.id for c in calls] or [None]))
                .group_by(CommunicationTurn.session_id)
            )
        ).all()
    }
    return {"sessions": [_session_row(c, int(counts.get(c.id, 0))) for c in calls]}


@router.get("/sessions/{session_id}", dependencies=[Depends(require_api_actor)])
async def session_detail(
    session_id: UUID, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    call = await session.get(CommunicationSession, session_id)
    if call is None:
        raise HTTPException(status_code=404, detail="session not found")
    turns = list(
        (
            await session.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.session_id == session_id)
                .order_by(CommunicationTurn.seq)
            )
        ).all()
    )
    return {
        **_session_row(call, len(turns)),
        "diagnostics": call.diagnostics,
        "rx_frame_stats": call.rx_frame_stats,
        "turns": [
            {
                "seq": t.seq,
                "speaker": t.speaker.value,
                "text": t.text,
                "asr_backend": t.asr_backend,
                "asr_confidence": t.asr_confidence,
                "occurred_at": t.occurred_at.isoformat(),
            }
            for t in turns
        ],
    }
