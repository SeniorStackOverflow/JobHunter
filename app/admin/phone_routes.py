from __future__ import annotations

# FastAPI's declarative dependency/form parameters intentionally call Depends/Form.
# ruff: noqa: B008
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
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


async def _call_detail_context(session: AsyncSession, session_id: str) -> dict[str, Any] | None:
    """Build the session-detail block for ``?view=calls&session=<uuid>``.

    An invalid or unknown ``session_id`` returns ``None`` so the caller falls
    through to the history list (never a 404/500).
    """
    from app.models.entities import AuditEvent, CommunicationSession, CommunicationTurn
    from app.models.enums import TurnSpeaker

    try:
        sid = UUID(session_id)
    except ValueError:
        return None

    call = await session.get(CommunicationSession, sid)
    if call is None:
        return None

    turns = list(
        (
            await session.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.session_id == call.id)
                .order_by(CommunicationTurn.seq)
            )
        ).all()
    )
    audits = list(
        (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.entity_id == str(call.id))
                .order_by(AuditEvent.timestamp)
            )
        ).all()
    )

    session_row = await _call_row(session, call)
    session_row.update(
        {
            "script_stage": call.script_stage,
            "diagnostics": call.diagnostics,
            "rx_frame_stats": call.rx_frame_stats,
        }
    )

    return {
        "session": session_row,
        "summary": call.summary or {},
        "summary_state": call.summary_state.value,
        "turns": [
            {
                "seq": t.seq,
                "speaker": t.speaker.value,
                "text": (t.spoken_text if t.speaker is TurnSpeaker.ASSISTANT else t.text),
                "delivery_status": t.delivery_status.value,
                "asr_confidence": t.asr_confidence,
                "audio_evidence_url": (
                    f"/admin/phone/evidence/{call.id}/{t.phonegate_transcript_id}.wav"
                    if t.audio_evidence_path
                    else None
                ),
            }
            for t in turns
        ],
        "audit_events": [{"action": a.action, "at": a.timestamp.isoformat()} for a in audits],
    }


async def _evidence_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """List every retained audio-evidence clip, newest sessions' turns first.

    Rows whose backing ``.wav`` file no longer exists on disk (retention sweep,
    manual cleanup) are silently skipped.
    """
    from app.models.entities import CanonicalJob, CommunicationSession, CommunicationTurn

    settings = get_settings()
    root = Path(settings.phone_evidence_dir)
    retention = timedelta(days=settings.phone_evidence_retention_days)

    turns = list(
        (
            await session.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.audio_evidence_path.is_not(None))
                .order_by(CommunicationTurn.session_id, CommunicationTurn.seq)
            )
        ).all()
    )

    rows: list[dict[str, Any]] = []
    for turn in turns:
        target = root / str(turn.session_id) / f"{turn.phonegate_transcript_id}.wav"
        try:
            mtime = target.stat().st_mtime
        except OSError:
            continue
        created_at = datetime.fromtimestamp(mtime, tz=UTC)

        call = await session.get(CommunicationSession, turn.session_id)
        company: str | None = None
        if call is not None and call.canonical_job_id is not None:
            job = await session.get(CanonicalJob, call.canonical_job_id)
            if job is not None:
                company = job.normalized_company

        rows.append(
            {
                "session_id": str(turn.session_id),
                "company": company,
                "started_at": call.started_at if call is not None else None,
                "seq": turn.seq,
                "text": turn.text,
                "asr_confidence": turn.asr_confidence,
                "created_at": created_at,
                "expires_at": created_at + retention,
                "url": (
                    f"/admin/phone/evidence/{turn.session_id}/{turn.phonegate_transcript_id}.wav"
                ),
            }
        )
    return rows


async def build_calls_context(
    session: AsyncSession,
    *,
    tab: str,
    page: int,
    filter_: str,
    query: str,
    session_id: str | None = None,
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
    if session_id is not None:
        ctx["detail"] = await _call_detail_context(session, session_id)

    if valid_tab == "evidence":
        ctx["evidence_rows"] = await _evidence_rows(session)
        return ctx

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


@router.get("/admin/phone/evidence/{session_id}/{transcript_id}.wav")
async def stream_evidence_clip(
    session_id: str,
    transcript_id: str,
    request: Request,
) -> Response:
    """Stream one retained audio-evidence clip to an authenticated admin.

    Both path params are validated structurally (UUID / digits) and the
    resolved file path is confirmed to live inside the evidence root before a
    single byte is read — a missing or escaping path is a 404, never a 500.
    """
    require_admin(request)
    try:
        sid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="запись недоступна") from exc
    if not transcript_id.isdigit():
        raise HTTPException(status_code=404, detail="запись недоступна")

    root = Path(get_settings().phone_evidence_dir).resolve()  # noqa: ASYNC240
    target = (root / str(sid) / f"{transcript_id}.wav").resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="запись недоступна")

    try:
        payload = target.read_bytes()
    except OSError as exc:
        # TOCTOU: prune_phone_evidence() (same ``phone`` queue) may unlink the
        # file between the is_file() check and this read — 404, never a 500.
        raise HTTPException(status_code=404, detail="запись недоступна") from exc

    return Response(
        payload,
        media_type="audio/wav",
        headers={"Cache-Control": "private, max-age=60"},
    )


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
