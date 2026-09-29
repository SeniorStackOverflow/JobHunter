from __future__ import annotations

# FastAPI declarative dependency/form defaults intentionally call Depends/Form.
# ruff: noqa: B008
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import (
    AccountAccessError,
    AccountService,
    IdentityAlreadyRegistered,
    InviteEmailMismatch,
    InviteInvalid,
    InviteService,
    InviteUnavailable,
    invite_state,
)
from app.admin.routes import templates as admin_templates
from app.audit import record_audit_event
from app.auth.access import (
    account_signer as _account_signer,
)
from app.auth.access import (
    csrf as _csrf,
)
from app.auth.access import (
    current_account as _current_account,
)
from app.auth.access import (
    has_admin_session as _has_admin_session,
)
from app.auth.access import (
    require_account as _require_account,
)
from app.auth.access import (
    require_user_csrf as _require_user_csrf,
)
from app.auth.access import (
    require_user_feature as _require_user_feature,
)
from app.auth.access import (
    secure_cookie as _secure_cookie,
)
from app.auth.access import (
    settings as _settings,
)
from app.auth.google import (
    IDENTITY_OAUTH_BINDING_COOKIE,
    IDENTITY_STATE_TTL_SECONDS,
    GoogleIdentityError,
    GoogleIdentityService,
)
from app.auth.workspace import render_user_dashboard
from app.auth.workspace import router as workspace_router
from app.database import get_session
from app.email.oauth import (
    GMAIL_OAUTH_BINDING_COOKIE,
    OAUTH_STATE_TTL_SECONDS,
    USER_GMAIL_OAUTH_ACTOR_PREFIX,
    GmailOAuthError,
    GmailOAuthService,
)
from app.models.entities import Account, Invite, UserProfile
from app.models.enums import AccountStatus, ProfileStatus
from app.profiles import ProfileService
from app.profiles.schemas import UserProfileInput
from app.security.auth import SessionSigner

router = APIRouter(tags=["user-auth"])
templates = Jinja2Templates(directory="app/auth/templates")

JOIN_COOKIE = "jobhunter_join"
JOIN_TTL_SECONDS = 30 * 60
_REGISTER_ACTOR_PREFIX = "register:"
_LOGIN_ACTOR = "login"


def _join_signer() -> SessionSigner:
    return SessionSigner(_settings().secret_key.get_secret_value(), salt="jobhunter-join")


async def _bound_invite(request: Request, session: AsyncSession) -> Invite:
    encoded = request.cookies.get(JOIN_COOKIE)
    invite_id_raw = (
        _join_signer().verify(encoded, JOIN_TTL_SECONDS) if encoded is not None else None
    )
    if invite_id_raw is None:
        raise InviteInvalid("invitation binding is missing")
    try:
        invite_id = UUID(invite_id_raw)
    except ValueError as exc:
        raise InviteInvalid("invitation binding is invalid") from exc
    invite = await session.get(Invite, invite_id)
    if invite is None or invite_state(invite) != "active":
        raise InviteUnavailable("invitation is not available")
    return invite


def _mask_email(value: str | None) -> str:
    if not value or "@" not in value:
        return "любой подтверждённый Google-аккаунт"
    local, domain = value.split("@", maxsplit=1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}***@{domain}"


@router.get("/")
async def public_root(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    settings = _settings()
    if settings.user_accounts_enabled and await _current_account(request, session) is not None:
        return RedirectResponse("/app", status_code=303)
    if _has_admin_session(request):
        return RedirectResponse("/admin", status_code=303)
    return RedirectResponse("/login", status_code=303)


@router.get("/join", response_class=HTMLResponse)
async def join_page(
    request: Request,
    token: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    _require_user_feature(registration=True)
    if token:
        try:
            invite = await InviteService().validate(session, token)
        except (InviteInvalid, InviteUnavailable):
            return templates.TemplateResponse(
                request=request,
                name="join.html",
                context={
                    "invite": None,
                    "error": "Приглашение недействительно или уже использовано.",
                },
                status_code=400,
            )
        response = RedirectResponse("/join", status_code=303)
        response.set_cookie(
            JOIN_COOKIE,
            _join_signer().issue(str(invite.id)),
            max_age=JOIN_TTL_SECONDS,
            secure=_secure_cookie(),
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    try:
        bound_invite: Invite | None = await _bound_invite(request, session)
    except (InviteInvalid, InviteUnavailable):
        bound_invite = None
    page_response = templates.TemplateResponse(
        request=request,
        name="join.html",
        context={
            "invite": bound_invite,
            "masked_email": (_mask_email(bound_invite.target_email) if bound_invite else None),
            "error": error,
        },
        status_code=200 if bound_invite is not None else 400,
    )
    page_response.headers["Cache-Control"] = "no-store"
    return page_response


@router.get("/auth/google/register")
async def google_register_start(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_user_feature(registration=True)
    try:
        invite = await _bound_invite(request, session)
    except (InviteInvalid, InviteUnavailable) as exc:
        raise HTTPException(status_code=400, detail="valid invitation required") from exc
    service = GoogleIdentityService(_settings())
    try:
        authorization = await service.create_authorization_request(
            session,
            actor=f"{_REGISTER_ACTOR_PREFIX}{invite.id}",
        )
    except GoogleIdentityError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail=exc.code) from exc
    await record_audit_event(
        session,
        actor="registration",
        action="account.registration_oauth_started",
        entity_type="invite",
        entity_id=str(invite.id),
        correlation_id=str(authorization.request_id),
        details={"target_bound": invite.target_email is not None},
    )
    await session.commit()
    response = RedirectResponse(authorization.authorization_url, status_code=302)
    response.set_cookie(
        IDENTITY_OAUTH_BINDING_COOKIE,
        authorization.binding_token,
        max_age=IDENTITY_STATE_TTL_SECONDS,
        path="/api/v1/oauth/gmail/callback",
        secure=service.secure_cookie,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/login", response_class=HTMLResponse)
async def user_login_page(
    request: Request,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    settings = _settings()
    if settings.user_accounts_enabled and await _current_account(request, session) is not None:
        return RedirectResponse("/app", status_code=303)
    if _has_admin_session(request):
        return RedirectResponse("/admin", status_code=303)
    google_identity = GoogleIdentityService(settings)
    response = admin_templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "login_mode": "unified",
            "error": error,
            "google_login_available": (
                google_identity.configured
                and (settings.user_accounts_enabled or bool(settings.google_admin_emails))
            ),
            "password_login_available": settings.admin_password_hash is not None,
            "csrf_token": _csrf().issue("login"),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/app/login")
async def legacy_user_login(error: str | None = None) -> RedirectResponse:
    target = "/login" if error is None else f"/login?error={error}"
    return RedirectResponse(target, status_code=303)


@router.get("/auth/google/login")
async def google_login_start(
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    service = GoogleIdentityService(_settings())
    try:
        authorization = await service.create_authorization_request(
            session,
            actor=_LOGIN_ACTOR,
        )
    except GoogleIdentityError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail=exc.code) from exc
    await session.commit()
    response = RedirectResponse(authorization.authorization_url, status_code=302)
    response.set_cookie(
        IDENTITY_OAUTH_BINDING_COOKIE,
        authorization.binding_token,
        max_age=IDENTITY_STATE_TTL_SECONDS,
        path="/api/v1/oauth/gmail/callback",
        secure=service.secure_cookie,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def complete_identity_callback(
    request: Request,
    *,
    state: str,
    session: AsyncSession,
) -> Response:
    settings = _settings()
    service = GoogleIdentityService(settings)
    binding_token = request.cookies.get(IDENTITY_OAUTH_BINDING_COOKIE)
    if not binding_token:
        return RedirectResponse("/login?error=invalid_identity_oauth_state", status_code=303)

    actor_hint: str | None = None
    account: Account | None = None
    admin_authenticated = False
    try:
        exchange = await service.exchange_callback(
            session,
            authorization_response=str(request.url),
            state=state,
            binding_token=binding_token,
        )
        actor_hint = exchange.actor
        if exchange.actor == _LOGIN_ACTOR:
            if exchange.identity.email in settings.google_admin_emails:
                admin_authenticated = True
                await record_audit_event(
                    session,
                    actor=f"google:{exchange.identity.email}",
                    action="admin.login.google",
                    entity_type="admin_session",
                    entity_id=exchange.identity.subject,
                    correlation_id=str(exchange.request_id),
                    decision="authenticated",
                    details={"provider": "google"},
                )
            else:
                if not settings.user_accounts_enabled:
                    raise AccountAccessError("user accounts are disabled")
                account = await AccountService().authenticate_google_identity(
                    session,
                    subject=exchange.identity.subject,
                    email=exchange.identity.email,
                    email_verified=True,
                )
                await record_audit_event(
                    session,
                    actor=f"account:{account.id}",
                    action="account.login",
                    entity_type="account",
                    entity_id=str(account.id),
                    correlation_id=str(exchange.request_id),
                    details={"provider": "google"},
                )
        elif exchange.actor.startswith(_REGISTER_ACTOR_PREFIX):
            if not settings.invite_registration_enabled:
                raise InviteUnavailable("registration is disabled")
            invite_id = UUID(exchange.actor.removeprefix(_REGISTER_ACTOR_PREFIX))
            account = await InviteService().redeem_bound_invite(
                session,
                invite_id=invite_id,
                subject=exchange.identity.subject,
                email=exchange.identity.email,
                email_verified=True,
            )
            await record_audit_event(
                session,
                actor=f"account:{account.id}",
                action="account.registered",
                entity_type="account",
                entity_id=str(account.id),
                correlation_id=str(exchange.request_id),
                details={"provider": "google"},
            )
        else:
            raise GoogleIdentityError("unknown identity actor", code="invalid_identity_actor")
        await session.commit()
    except (
        AccountAccessError,
        GoogleIdentityError,
        IdentityAlreadyRegistered,
        InviteEmailMismatch,
        InviteInvalid,
        InviteUnavailable,
        ValueError,
    ) as exc:
        await session.rollback()
        if isinstance(exc, AccountAccessError):
            if str(exc) == "Google identity is not registered":
                code = "identity_not_registered"
            elif str(exc) == "account is not active":
                code = "account_inactive"
            else:
                code = "account_access_denied"
        else:
            code = getattr(exc, "code", None) or type(exc).__name__.casefold()
        target = "/join" if (actor_hint or "").startswith(_REGISTER_ACTOR_PREFIX) else "/login"
        response = RedirectResponse(f"{target}?error={code}", status_code=303)
    else:
        if admin_authenticated:
            response = RedirectResponse("/admin", status_code=303)
            response.set_cookie(
                settings.session_cookie_name,
                SessionSigner(settings.secret_key.get_secret_value()).issue(
                    settings.admin_username
                ),
                max_age=settings.session_ttl_seconds,
                secure=_secure_cookie(),
                httponly=True,
                samesite="lax",
                path="/",
            )
        else:
            assert account is not None
            response = RedirectResponse("/app", status_code=303)
            response.set_cookie(
                settings.user_session_cookie_name,
                _account_signer().issue(account.id, account.session_version),
                max_age=settings.user_session_ttl_seconds,
                secure=_secure_cookie(),
                httponly=True,
                samesite="lax",
                path="/",
            )
            response.delete_cookie(JOIN_COOKIE, path="/")
    response.delete_cookie(
        IDENTITY_OAUTH_BINDING_COOKIE,
        path="/api/v1/oauth/gmail/callback",
        secure=service.secure_cookie,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/app", response_class=HTMLResponse)
async def user_home(
    request: Request,
    view: str = "overview",
    profile_id: UUID | None = None,
    page: int = 1,
    notice: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    _require_user_feature()
    current = await _current_account(request, session)
    if current is None:
        return RedirectResponse("/login", status_code=303)
    account, session_token = current
    return await render_user_dashboard(
        request,
        session,
        account,
        session_token,
        view=view,
        profile_id=profile_id,
        page=page,
        notice=notice,
    )


@router.post("/app/profiles")
async def create_user_profile(
    request: Request,
    name: str = Form(...),
    contact_email: str | None = Form(None),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_user_feature()
    _require_user_csrf(request, csrf_token)
    account, _ = await _require_account(request, session)
    locked = await session.scalar(select(Account).where(Account.id == account.id).with_for_update())
    if locked is None or locked.status != AccountStatus.ACTIVE:
        raise HTTPException(status_code=401, detail="account unavailable")
    count = int(
        await session.scalar(
            select(func.count(UserProfile.id)).where(
                UserProfile.owner_account_id == account.id,
                UserProfile.status != ProfileStatus.ARCHIVED,
            )
        )
        or 0
    )
    if count >= locked.max_profiles:
        raise HTTPException(status_code=409, detail="profile limit reached")
    payload = UserProfileInput(
        name=name.strip(),
        contact_email=contact_email.strip() if contact_email else None,
    )
    profile = await ProfileService().create_profile(
        session,
        payload,
        owner_account_id=account.id,
        status=ProfileStatus.DRAFT,
        make_default=count == 0,
    )
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="profile.created",
        entity_type="user_profile",
        entity_id=str(profile.id),
        correlation_id=str(profile.id),
        decision="draft",
    )
    await session.commit()
    return RedirectResponse(
        f"/app?view=settings&profile_id={profile.id}&notice=profile_created", status_code=303
    )


@router.post("/app/invites")
async def create_user_invite(
    request: Request,
    target_email: str = Form(...),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> Response:
    _require_user_feature()
    _require_user_csrf(request, csrf_token)
    account, session_token = await _require_account(request, session)
    try:
        created = await InviteService().create(
            session,
            creator_account_id=account.id,
            target_email=target_email,
        )
    except InviteUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="invite.created",
        entity_type="invite",
        entity_id=str(created.invite.id),
        correlation_id=str(created.invite.id),
        details={"target_email": created.invite.target_email},
    )
    await session.commit()
    return await render_user_dashboard(
        request,
        session,
        account,
        session_token,
        view="settings",
        notice="invite_created",
        invite_token=created.token,
    )


@router.get("/app/gmail/connect")
async def user_gmail_connect(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_user_feature()
    account, _ = await _require_account(request, session)
    service = GmailOAuthService(_settings())
    try:
        authorization = await service.create_authorization_request(
            session,
            actor=f"{USER_GMAIL_OAUTH_ACTOR_PREFIX}{account.id}",
            account_id=account.id,
        )
    except GmailOAuthError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail=exc.code) from exc
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="oauth.gmail.started",
        entity_type="oauth_authorization_request",
        entity_id=str(authorization.request_id),
        correlation_id=str(authorization.request_id),
        decision="redirected",
        details={"provider": "gmail"},
    )
    await session.commit()
    response = RedirectResponse(authorization.authorization_url, status_code=302)
    response.set_cookie(
        GMAIL_OAUTH_BINDING_COOKIE,
        authorization.binding_token,
        max_age=OAUTH_STATE_TTL_SECONDS,
        path="/api/v1/oauth/gmail/callback",
        secure=service.secure_cookie,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/app/gmail/disconnect")
async def user_gmail_disconnect(
    request: Request,
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_user_feature()
    _require_user_csrf(request, csrf_token)
    account, _ = await _require_account(request, session)
    service = GmailOAuthService(_settings())
    was_connected = await service.disconnect(session, account_id=account.id)
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="oauth.gmail.disconnected",
        entity_type="oauth_credential",
        entity_id=str(account.id),
        correlation_id=str(account.id),
        decision="disconnected" if was_connected else "already_disconnected",
        details={"provider": "gmail", "remote_grant_revoked": False},
    )
    await session.commit()
    return RedirectResponse("/app?notice=gmail_disconnected", status_code=303)


@router.post("/app/logout")
async def user_logout(
    request: Request,
    csrf_token: str = Form(...),
) -> RedirectResponse:
    _require_user_feature()
    _require_user_csrf(request, csrf_token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(_settings().user_session_cookie_name, path="/")
    return response


router.include_router(workspace_router)


__all__ = [
    "JOIN_COOKIE",
    "JOIN_TTL_SECONDS",
    "complete_identity_callback",
    "router",
]
