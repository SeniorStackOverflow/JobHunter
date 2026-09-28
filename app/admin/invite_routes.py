from __future__ import annotations

# FastAPI declarative dependency/form defaults intentionally call Depends/Form.
# ruff: noqa: B008
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import InviteService, InviteUnavailable, invite_state
from app.admin.routes import (
    _audit_admin,
    _csrf,
    _session_token,
    require_admin,
    require_admin_page,
    require_csrf,
    templates,
)
from app.database import get_session
from app.models.constants import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.models.entities import Invite

router = APIRouter()


@router.get("/admin/invites", response_class=HTMLResponse)
async def admin_invites(
    request: Request,
    _: str = Depends(require_admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    values = list(
        (
            await session.scalars(
                select(Invite)
                .where(Invite.created_by_account_id == BOOTSTRAP_ADMIN_ACCOUNT_ID)
                .order_by(Invite.created_at.desc(), Invite.id.desc())
            )
        ).all()
    )
    response = templates.TemplateResponse(
        request=request,
        name="invites.html",
        context={
            "invites": values,
            "invite_state": invite_state,
            "csrf_token": _csrf().issue(_session_token(request)),
            "created_link": None,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/admin/invites", response_class=HTMLResponse)
async def create_admin_invite(
    request: Request,
    target_email: str = Form(""),
    ttl_days: int = Form(14),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    require_csrf(request, csrf_token)
    ttl_days = max(1, min(ttl_days, 30))
    try:
        created = await InviteService().create(
            session,
            creator_account_id=BOOTSTRAP_ADMIN_ACCOUNT_ID,
            target_email=target_email.strip() or None,
            ttl=timedelta(days=ttl_days),
        )
    except InviteUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _audit_admin(
        session,
        "invite.created",
        "invite",
        str(created.invite.id),
        details={
            "target_email": created.invite.target_email,
            "expires_at": created.invite.expires_at.isoformat(),
        },
    )
    await session.commit()
    values = list(
        (
            await session.scalars(
                select(Invite)
                .where(Invite.created_by_account_id == BOOTSTRAP_ADMIN_ACCOUNT_ID)
                .order_by(Invite.created_at.desc(), Invite.id.desc())
            )
        ).all()
    )
    base = str(request.base_url).rstrip("/")
    response = templates.TemplateResponse(
        request=request,
        name="invites.html",
        context={
            "invites": values,
            "invite_state": invite_state,
            "csrf_token": _csrf().issue(_session_token(request)),
            "created_link": f"{base}/join?token={created.token}",
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/admin/invites/{invite_id}/revoke")
async def revoke_admin_invite(
    invite_id: str,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    from uuid import UUID

    require_csrf(request, csrf_token)
    try:
        value = UUID(invite_id)
        invite = await InviteService().revoke(
            session,
            invite_id=value,
            actor_account_id=BOOTSTRAP_ADMIN_ACCOUNT_ID,
        )
    except (ValueError, InviteUnavailable) as exc:
        raise HTTPException(status_code=409, detail="invite cannot be revoked") from exc
    await _audit_admin(
        session,
        "invite.revoked",
        "invite",
        str(invite.id),
        decision="revoked",
    )
    await session.commit()
    return RedirectResponse("/admin/invites", status_code=303)


__all__ = ["router"]
