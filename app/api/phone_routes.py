from __future__ import annotations

# ruff: noqa: B008
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_actor
from app.database import get_session
from app.models.entities import CommunicationSession, CommunicationTurn, PhoneChannelHealth
from app.phone.health import HealthComponent, channel_status
from app.phone.numbers import mask_phone

router = APIRouter(prefix="/api/v1/phone", tags=["phone"])


@router.get("/status", dependencies=[Depends(require_api_actor)])
async def phone_status(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = list((await session.scalars(select(PhoneChannelHealth))).all())
    components = [HealthComponent(r.component, r.status, r.detail, r.last_ok_at) for r in rows]
    agent_row = next((r for r in rows if r.component == "agent"), None)
    newest = await session.scalar(
        select(CommunicationSession).order_by(desc(CommunicationSession.started_at)).limit(1)
    )
    return {
        "channel": channel_status(components).value if components else "unknown",
        "agent": {
            "last_ok_at": agent_row.last_ok_at.isoformat()
            if agent_row and agent_row.last_ok_at
            else None,
            "status": agent_row.status.value if agent_row else "unknown",
        },
        "components": [
            {
                "component": r.component,
                "status": r.status.value,
                "detail": r.detail,
                "last_ok_at": r.last_ok_at.isoformat() if r.last_ok_at else None,
            }
            for r in rows
        ],
        "current_call": {
            "session_id": str(newest.id) if newest else None,
            "state": "connected"
            if newest and newest.ended_at is None and newest.answered_at is not None
            else ("ringing" if newest and newest.ended_at is None else "idle"),
            "caller_number": mask_phone(newest.remote_address) if newest else None,
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
