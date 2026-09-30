from __future__ import annotations

# FastAPI declarative dependency/form defaults intentionally call Depends/Form.
# ruff: noqa: B008
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
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
from app.email.oauth import GmailOAuthService
from app.models.constants import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.models.entities import (
    Account,
    AccountIdentity,
    Application,
    CommunicationSession,
    Invite,
    JobPreference,
    JobSource,
    ProfileSourcePreference,
    UserProfile,
)
from app.models.enums import (
    AccountStatus,
    ApplicationStatus,
    CommunicationChannel,
    PhoneVerificationStatus,
    ProfileStatus,
    SourceHealth,
)
from app.profiles import ProfileService
from app.settings import get_settings
from app.ui.panel import Panel
from app.ui.presentation import _LOCAL_TZ
from app.ui.status import attention_status, panel_notifications, profile_attention

router = APIRouter()


async def _accounts_context(
    request: Request,
    session: AsyncSession,
    *,
    created_link: str | None = None,
) -> dict[str, object]:
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
    invites = list(
        (
            await session.scalars(
                select(Invite)
                .where(Invite.created_by_account_id == BOOTSTRAP_ADMIN_ACCOUNT_ID)
                .order_by(Invite.created_at.desc(), Invite.id.desc())
            )
        ).all()
    )
    selected_profile = await ProfileService().get_profile(session)
    pending_review = 0
    sources: list[JobSource] = []
    disabled_source_ids = set()
    enabled_sources = healthy_sources = 0
    if selected_profile is not None:
        pending_review = int(
            await session.scalar(
                select(func.count(Application.id)).where(
                    Application.profile_id == selected_profile.id,
                    Application.status == ApplicationStatus.PENDING_REVIEW,
                )
            )
            or 0
        )
        disabled_source_ids = {
            row.source_id
            for row in (
                await session.scalars(
                    select(ProfileSourcePreference).where(
                        ProfileSourcePreference.profile_id == selected_profile.id,
                        ProfileSourcePreference.enabled.is_(False),
                    )
                )
            ).all()
        }
        sources = list((await session.scalars(select(JobSource).order_by(JobSource.name))).all())
        enabled_sources = sum(
            source.enabled and source.id not in disabled_source_ids for source in sources
        )
        healthy_sources = sum(
            source.enabled
            and source.id not in disabled_source_ids
            and source.health_status == SourceHealth.HEALTHY
            for source in sources
        )
    phone_review = int(
        await session.scalar(
            select(func.count(CommunicationSession.id)).where(
                CommunicationSession.channel == CommunicationChannel.CALL,
                or_(
                    CommunicationSession.needs_review.is_(True),
                    CommunicationSession.verification_status
                    == PhoneVerificationStatus.NEEDS_REVIEW,
                ),
            )
        )
        or 0
    )
    panel = Panel(is_admin=True, profile_id=selected_profile.id if selected_profile else None)
    counts = {
        "pending_review": pending_review,
        "phone_review": phone_review,
        "enabled_sources": enabled_sources,
        "healthy_sources": healthy_sources,
        "unhealthy_sources": enabled_sources - healthy_sources,
    }
    attention = []
    overview = {"sent_today": 0, "daily_limit": 0}
    if selected_profile is not None:
        service = ProfileService()
        preferences = await service.get_preferences(session, selected_profile.id)
        gmail_oauth = await GmailOAuthService(get_settings()).get_status(
            session, account_id=selected_profile.owner_account_id
        )
        attention = await profile_attention(
            session,
            panel,
            selected_profile,
            preferences,
            counts,
            sources,
            disabled_source_ids,
            gmail_oauth,
        )
        local_now = datetime.now(_LOCAL_TZ)
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        overview = {
            "sent_today": int(
                await session.scalar(
                    select(func.count(Application.id)).where(
                        Application.profile_id == selected_profile.id, Application.sent_at >= start
                    )
                )
                or 0
            ),
            "daily_limit": preferences.maximum_daily_applications,
        }
    notifications = await panel_notifications(session, panel, attention)
    counts["notification_count"] = len(notifications)
    tone, title = attention_status(notifications)
    return {
        "panel": panel,
        "view_title": "Пользователи",
        "delivery_enabled": get_settings().real_email_delivery_enabled,
        "accounts": accounts,
        "identities_by_account": identities_by_account,
        "profiles_by_account": profiles_by_account,
        "invites": invites,
        "invite_state": invite_state,
        "created_link": created_link,
        "csrf_token": _csrf().issue(_session_token(request)),
        "view": "accounts",
        "selected_profile_id": selected_profile.id if selected_profile is not None else None,
        "counts": counts,
        "notifications": notifications,
        "overall_tone": tone,
        "overall_title": title,
        "overview": overview,
    }


@router.get("/admin/invites")
async def admin_invites(
    _: str = Depends(require_admin_page),
) -> RedirectResponse:
    return RedirectResponse("/admin/accounts", status_code=303)


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
    base = str(request.base_url).rstrip("/")
    response = templates.TemplateResponse(
        request=request,
        name="accounts.html",
        context=await _accounts_context(
            request,
            session,
            created_link=f"{base}/join?token={created.token}",
        ),
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
    return RedirectResponse("/admin/accounts", status_code=303)


@router.get("/admin/accounts", response_class=HTMLResponse)
async def admin_accounts(
    request: Request,
    _: str = Depends(require_admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    response = templates.TemplateResponse(
        request=request,
        name="accounts.html",
        context=await _accounts_context(request, session),
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
