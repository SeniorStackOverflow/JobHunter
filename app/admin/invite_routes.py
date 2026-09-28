from __future__ import annotations

# FastAPI declarative dependency/form defaults intentionally call Depends/Form.
# ruff: noqa: B008
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import AccountService, InviteService, InviteUnavailable, invite_state
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
from app.models.entities import Account, AccountIdentity, Invite, JobPreference, UserProfile
from app.models.enums import AccountStatus, ProfileStatus

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


@router.get("/admin/accounts", response_class=HTMLResponse)
async def admin_accounts(
    request: Request,
    _: str = Depends(require_admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    accounts = list(
        (
            await session.scalars(
                select(Account)
                .where(Account.id != BOOTSTRAP_ADMIN_ACCOUNT_ID)
                .order_by(Account.created_at.desc(), Account.id.desc())
            )
        ).all()
    )
    account_ids = [account.id for account in accounts]
    identities_by_account: dict[object, AccountIdentity] = {}
    profiles_by_account: dict[object, list[UserProfile]] = {}
    if account_ids:
        for identity in (
            await session.scalars(
                select(AccountIdentity).where(AccountIdentity.account_id.in_(account_ids))
            )
        ).all():
            identities_by_account[identity.account_id] = identity
        for profile in (
            await session.scalars(
                select(UserProfile)
                .where(UserProfile.owner_account_id.in_(account_ids))
                .order_by(UserProfile.created_at, UserProfile.id)
            )
        ).all():
            profiles_by_account.setdefault(profile.owner_account_id, []).append(profile)
    response = templates.TemplateResponse(
        request=request,
        name="accounts.html",
        context={
            "accounts": accounts,
            "identities_by_account": identities_by_account,
            "profiles_by_account": profiles_by_account,
            "csrf_token": _csrf().issue(_session_token(request)),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/admin/accounts/{account_id}/suspend")
async def suspend_account(
    account_id: str,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    from uuid import UUID

    require_csrf(request, csrf_token)
    try:
        value = UUID(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="account not found") from exc
    if value == BOOTSTRAP_ADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=409, detail="bootstrap admin cannot be suspended")
    try:
        account = await AccountService().suspend(session, value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="account not found") from exc
    await _audit_admin(
        session,
        "account.suspended",
        "account",
        str(account.id),
        decision="suspended",
    )
    await session.commit()
    return RedirectResponse("/admin/accounts", status_code=303)


@router.post("/admin/accounts/{account_id}/reactivate")
async def reactivate_account(
    account_id: str,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    from uuid import UUID

    require_csrf(request, csrf_token)
    try:
        value = UUID(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="account not found") from exc
    if value == BOOTSTRAP_ADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=409, detail="bootstrap admin cannot be changed")
    account = await session.scalar(select(Account).where(Account.id == value).with_for_update())
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    if account.status == AccountStatus.DISABLED:
        raise HTTPException(status_code=409, detail="disabled account cannot be reactivated")
    account.status = AccountStatus.ACTIVE
    await _audit_admin(
        session,
        "account.reactivated",
        "account",
        str(account.id),
        decision="active",
    )
    await session.commit()
    return RedirectResponse("/admin/accounts", status_code=303)


@router.post("/admin/accounts/{account_id}/limits")
async def update_account_limits(
    account_id: str,
    request: Request,
    invite_allowance: int = Form(...),
    max_profiles: int = Form(...),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    from uuid import UUID

    require_csrf(request, csrf_token)
    try:
        value = UUID(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="account not found") from exc
    if value == BOOTSTRAP_ADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=409, detail="bootstrap admin limits are fixed")
    if not 0 <= invite_allowance <= 20 or not 1 <= max_profiles <= 5:
        raise HTTPException(status_code=422, detail="account limits are out of range")
    account = await session.scalar(select(Account).where(Account.id == value).with_for_update())
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    account.invite_allowance = invite_allowance
    account.max_profiles = max_profiles
    await _audit_admin(
        session,
        "account.limits_updated",
        "account",
        str(account.id),
        details={
            "invite_allowance": invite_allowance,
            "max_profiles": max_profiles,
        },
    )
    await session.commit()
    return RedirectResponse("/admin/accounts", status_code=303)


@router.post("/admin/profiles/{profile_id}/status")
async def update_profile_status(
    profile_id: str,
    request: Request,
    profile_status: str = Form(...),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    from uuid import UUID

    require_csrf(request, csrf_token)
    try:
        value = UUID(profile_id)
        new_status = ProfileStatus(profile_status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid profile status") from exc
    if new_status not in {ProfileStatus.ACTIVE, ProfileStatus.PAUSED}:
        raise HTTPException(status_code=422, detail="unsupported profile status")
    profile = await session.scalar(
        select(UserProfile).where(UserProfile.id == value).with_for_update()
    )
    if profile is None or profile.owner_account_id == BOOTSTRAP_ADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=404, detail="user profile not found")
    profile.status = new_status
    preference = await session.scalar(
        select(JobPreference).where(JobPreference.profile_id == profile.id)
    )
    if preference is not None and new_status == ProfileStatus.PAUSED:
        preference.global_pause = True
    await _audit_admin(
        session,
        "profile.status_updated",
        "user_profile",
        str(profile.id),
        decision=new_status.value,
        details={"owner_account_id": str(profile.owner_account_id)},
    )
    await session.commit()
    return RedirectResponse("/admin/accounts", status_code=303)


__all__ = ["router"]
