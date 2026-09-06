from __future__ import annotations

# FastAPI's declarative dependency/form parameters intentionally call Depends/Form.
# ruff: noqa: B008
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Select

# ``routes`` is imported as a module (not ``from ... import _phone_redis``) on
# purpose: ``phone_health_context`` and the POST handlers read
# ``_admin_routes._phone_redis`` / ``_admin_routes._audit_admin`` through the
# module object so tests can ``monkeypatch.setattr(admin_routes, "_phone_redis", ...)``
# and have the override take effect here.
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


_CALLS_PER_PAGE = 25
_CALLS_TABS = {"live", "history", "evidence"}


def _apply_call_filter(stmt: Select[Any], filter_: str) -> Select[Any]:
    """Narrow a ``communication_sessions`` query by the history filter.

    RULING R2: this lives here for now; Task 15 lifts it into a shared helper.
    ``interview_proposed`` is intentionally not handled in SQL — see
    ``build_calls_context`` for why it is filtered in Python instead.
    """
    from app.models.entities import CommunicationSession
    from app.models.enums import CommunicationOutcome

    if filter_ == "needs_review":
        return stmt.where(CommunicationSession.needs_review.is_(True))
    if filter_ == "missed_dropped":
        return stmt.where(
            CommunicationSession.outcome.in_(
                [CommunicationOutcome.MISSED, CommunicationOutcome.ABANDONED]
            )
        )
    if filter_ == "unknown_caller":
        return stmt.where(CommunicationSession.application_id.is_(None))
    return stmt


async def _call_row(session: AsyncSession, row: Any) -> dict[str, Any]:
    from app.models.entities import CanonicalJob
    from app.phone.numbers import mask_phone

    company: str | None = None
    vacancy: str | None = None
    if row.canonical_job_id is not None:
        job = await session.get(CanonicalJob, row.canonical_job_id)
        if job is not None:
            company = job.normalized_company
            vacancy = job.normalized_title
    duration_s: int | None = None
    if row.ended_at is not None and row.started_at is not None:
        duration_s = int((row.ended_at - row.started_at).total_seconds())
    summary: dict[str, Any] = row.summary or {}
    return {
        "id": str(row.id),
        "started_at": row.started_at,
        "company": company,
        "vacancy": vacancy,
        "caller": mask_phone(row.remote_address),
        "direction": row.direction.value,
        "duration_s": duration_s,
        "outcome": row.outcome.value if row.outcome is not None else None,
        "auto_answered": row.auto_answered,
        "needs_review": row.needs_review,
        "summary_state": row.summary_state.value,
        "telegram_state": (summary.get("telegram") or {}).get("state"),
    }


async def build_calls_context(
    session: AsyncSession,
    *,
    tab: str,
    page: int,
    filter_: str,
    query: str,
) -> dict[str, Any]:
    """Assemble the ``?view=calls`` template context (Live + История tabs)."""
    from app.models.entities import CanonicalJob, CommunicationSession
    from app.models.enums import CommunicationChannel

    valid_tab = tab if tab in _CALLS_TABS else "live"
    health = await phone_health_context(session)
    ctx: dict[str, Any] = {
        "tab": valid_tab,
        "filter": filter_,
        "query": query,
        "detail": None,
        "calls_health": health,
        "active_call": health.get("active_call"),
        "call_rows": [],
        "pagination": _admin_routes._pagination(0, 1, _CALLS_PER_PAGE),
    }
    if valid_tab != "history":
        return ctx

    stmt = select(CommunicationSession).where(
        CommunicationSession.channel == CommunicationChannel.CALL
    )
    if query:
        like = f"%{query}%"
        stmt = stmt.outerjoin(
            CanonicalJob, CommunicationSession.canonical_job_id == CanonicalJob.id
        ).where(
            or_(
                CanonicalJob.normalized_company.ilike(like),
                CanonicalJob.normalized_title.ilike(like),
                CommunicationSession.remote_address.ilike(like),
            )
        )
    stmt = _apply_call_filter(stmt, filter_).order_by(CommunicationSession.started_at.desc())

    if filter_ == "interview_proposed":
        # JSON-path access (summary -> hints -> outcome_guess) is not portable
        # between SQLite (unit tests) and Postgres (prod), so fetch the filtered
        # set and narrow it in Python before paginating the resulting list.
        matched = [
            row
            for row in (await session.scalars(stmt)).all()
            if ((row.summary or {}).get("hints") or {}).get("outcome_guess") == "interview_proposed"
        ]
        pagination = _admin_routes._pagination(len(matched), page, _CALLS_PER_PAGE)
        start = (int(pagination["page"]) - 1) * _CALLS_PER_PAGE
        page_rows = matched[start : start + _CALLS_PER_PAGE]
    else:
        total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        pagination = _admin_routes._pagination(total, page, _CALLS_PER_PAGE)
        offset = (int(pagination["page"]) - 1) * _CALLS_PER_PAGE
        page_rows = list((await session.scalars(stmt.limit(_CALLS_PER_PAGE).offset(offset))).all())

    ctx["pagination"] = pagination
    ctx["call_rows"] = [await _call_row(session, row) for row in page_rows]
    return ctx


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
