from __future__ import annotations

import contextlib

# FastAPI's declarative dependency/form parameters intentionally call Depends/File.
# ruff: noqa: B008
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications import (
    ApplicationService,
    DeliveryReconciliationError,
    get_application_detail,
    reconcile_stale_delivery_unknown,
)
from app.applications.service import ApplicationPreparationError
from app.audit import record_audit_event
from app.crawlers.pipeline import ScanService
from app.crawlers.registry import build_default_registry
from app.crawlers.source_control import (
    SourceControlError,
    disable_source_record,
    enable_source_record,
)
from app.database import get_session
from app.email.oauth import (
    GMAIL_OAUTH_BINDING_COOKIE,
    GOOGLE_ADMIN_OAUTH_ACTOR,
    OAUTH_STATE_TTL_SECONDS,
    GmailOAuthError,
    GmailOAuthService,
)
from app.email.service import EmailSendBlocked, EmailService
from app.learning import (
    ReviewLearningService,
    fixed_preference_dimensions,
)
from app.models.entities import (
    Alert,
    Application,
    JobSource,
    Resume,
    ScanRun,
    UserProfile,
)
from app.models.enums import (
    ReviewOutcome,
    ReviewReason,
    RunStatus,
    ScanType,
)
from app.profiles import ProfileService, ResumeService
from app.profiles.schemas import JobPreferenceUpdateInput, UserProfileInput
from app.profiles.service import ResumeDeletion, ResumeInUseError
from app.profiles.sources import set_source_selected
from app.security.auth import CsrfProtector, SessionSigner, verify_password
from app.security.files import (
    UnsafeResumeError,
    read_verified_resume,
    validate_resume_upload,
)
from app.settings import get_settings
from app.ui.presentation import (
    _FEEDBACK_NOTICE_TONES as _FEEDBACK_NOTICE_TONES,
)
from app.ui.presentation import (
    _FEEDBACK_NOTICES as _FEEDBACK_NOTICES,
)
from app.ui.presentation import (
    _VIEW_TITLES as _VIEW_TITLES,
)
from app.ui.presentation import (
    _admin_asset_url as _admin_asset_url,
)
from app.ui.presentation import (
    _application_approval_issue as _application_approval_issue,
)
from app.ui.presentation import (
    _approval_failure_notice as _approval_failure_notice,
)
from app.ui.presentation import (
    _format_dt as _format_dt,
)
from app.ui.presentation import (
    _pagination as _pagination,
)
from app.ui.presentation import (
    _safe_external_link as _safe_external_link,
)
from app.ui.presentation import (
    _status_label as _status_label,
)
from app.ui.presentation import (
    _status_tone as _status_tone,
)
from app.ui.presentation import (
    templates as templates,
)

router = APIRouter(tags=["admin"])
logger = structlog.get_logger(__name__)


def _signer() -> SessionSigner:
    return SessionSigner(get_settings().secret_key.get_secret_value())


def _phone_redis() -> Any:
    from redis.asyncio import Redis as AsyncRedis

    return AsyncRedis.from_url(get_settings().redis_url, decode_responses=True)


def _csrf() -> CsrfProtector:
    return CsrfProtector(get_settings().secret_key.get_secret_value())


def _session_token(request: Request) -> str:
    token = request.cookies.get(get_settings().session_cookie_name)
    subject = _signer().verify(token, get_settings().session_ttl_seconds) if token else None
    if subject != get_settings().admin_username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
    assert token is not None
    from app.auth.session import remember_session

    remember_session(request, get_settings(), token)
    return token


def require_admin(request: Request) -> str:
    _session_token(request)
    return get_settings().admin_username


def require_admin_page(request: Request) -> str:
    """Browser-facing admin GETs redirect unauthenticated users to login."""
    try:
        return require_admin(request)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
            ) from exc
        raise


def require_csrf(request: Request, csrf_token: str) -> None:
    session_token = _session_token(request)
    if not _csrf().verify(csrf_token, session_token, get_settings().csrf_ttl_seconds):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid CSRF token")


async def _audit_admin(
    session: AsyncSession,
    action: str,
    entity_type: str,
    entity_id: str,
    *,
    decision: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    await record_audit_event(
        session,
        actor="admin",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        correlation_id=entity_id,
        decision=decision,
        details=details,
    )


@router.get("/admin/login")
async def legacy_admin_login(oauth_error: str | None = None) -> RedirectResponse:
    target = "/login"
    if oauth_error:
        target = f"/login?error={quote(oauth_error)}"
    return RedirectResponse(target, status_code=303)


@router.get("/admin/auth/google")
async def legacy_google_admin_login_start(consent: bool = False) -> RedirectResponse:
    if consent:
        return RedirectResponse("/admin/oauth/gmail/connect", status_code=303)
    return RedirectResponse("/auth/google/login", status_code=303)


@router.get("/admin/oauth/gmail/connect")
async def admin_gmail_connect(
    actor: str = Depends(require_admin_page),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    service = GmailOAuthService(get_settings())
    try:
        authorization = await service.create_authorization_request(
            session, actor=GOOGLE_ADMIN_OAUTH_ACTOR, force_consent=True
        )
    except GmailOAuthError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail=exc.code) from exc
    await _audit_admin(
        session,
        "oauth.gmail.started",
        "oauth_authorization_request",
        str(authorization.request_id),
        decision="redirected",
        details={"provider": "gmail", "actor": actor},
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


@router.post("/admin/login")
async def login(
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    settings = get_settings()
    csrf_valid = _csrf().verify(csrf_token, "login", settings.csrf_ttl_seconds)
    password_valid = bool(
        settings.admin_password_hash
        and verify_password(password, settings.admin_password_hash.get_secret_value())
    )
    if not csrf_valid:
        return RedirectResponse("/login?error=admin_login_expired", status_code=303)
    if not password_valid:
        return RedirectResponse("/login?error=admin_password_invalid", status_code=303)
    token = _signer().issue(settings.admin_username)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_seconds,
        secure=settings.public_base_url.casefold().startswith("https://"),
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/admin/logout")
async def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    require_csrf(request, csrf_token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return response


@router.get("/admin", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    profile_id: UUID | None = None,
    view: str = "overview",
    page: int = 1,
    q: str = "",
    status_filter: str = "pending_review",
    history_kind: str = "sent",
    notice: str | None = None,
    google: str | None = None,
    _: str = Depends(require_admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    from app.ui.dashboard import render_dashboard

    if view == "diagnostics":
        target = "/admin?view=history&history_kind=alerts"
        if profile_id is not None:
            target += f"&profile_id={profile_id}"
        return RedirectResponse(target, status_code=303)

    return await render_dashboard(
        request,
        session,
        get_settings(),
        _session_token(request),
        profile_id=profile_id,
        view=view,
        page=page,
        q=q,
        status_filter=status_filter,
        history_kind=history_kind,
        notice=notice,
        google=google,
    )


def _items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _daily_application_rules(
    existing: dict[str, Any] | None,
    *,
    minimum: int,
    force_minimum: bool,
) -> dict[str, Any]:
    """Merge admin-owned daily targets without discarding other advanced rules."""

    rules = dict(existing or {})
    rules["minimum_daily_applications"] = minimum
    # Minimum is an operational requirement whenever it is configured.
    # Keep the legacy key true for backward-compatible consumers.
    rules["force_minimum_daily_applications"] = minimum > 0
    return rules


@router.post("/admin/profile")
async def save_profile(
    request: Request,
    name: str = Form(...),
    contact_email: str = Form(""),
    phone: str = Form(""),
    location: str = Form(""),
    languages: str = Form(""),
    skills: str = Form(""),
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    existing = await ProfileService().get_profile(session, profile_id)
    payload = UserProfileInput(
        name=name,
        contact_email=contact_email or None,
        phone=phone or None,
        location=location or None,
        languages=[{"code": item, "confirmed": True} for item in _items(languages)],
        skills=_items(skills),
        work_experience=existing.work_experience if existing else [],
        education=existing.education if existing else [],
        driving_licences=existing.driving_licences if existing else [],
        confirmed_facts=existing.confirmed_facts if existing else [],
        availability=existing.availability if existing else {},
    )
    profile = await ProfileService().upsert_profile(session, payload, profile_id)
    await _audit_admin(session, "profile.updated", "user_profile", str(profile.id))
    await session.commit()
    return RedirectResponse(
        f"/admin?view=settings&profile_id={profile.id}&notice=profile_saved", status_code=303
    )


@router.post("/admin/profiles")
async def create_profile(
    request: Request,
    name: str = Form(...),
    make_default: bool = Form(False),
    resume_name: str = Form(""),
    resume_category: str = Form(""),
    resume_file: UploadFile | None = File(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    settings = get_settings()
    has_resume = resume_file is not None and bool(resume_file.filename)
    if has_resume and not (resume_name.strip() and resume_category.strip()):
        raise HTTPException(
            status_code=422, detail="resume name and category are required with a file"
        )
    data = b""
    if has_resume:
        assert resume_file is not None
        data = await resume_file.read(settings.max_resume_bytes + 1)
        try:
            validate_resume_upload(
                resume_file.filename or "resume.pdf",
                resume_file.content_type or "",
                data,
                settings.max_resume_bytes,
            )
        except UnsafeResumeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    resume_service = ResumeService(settings)
    resume: Resume | None = None
    try:
        profile = await ProfileService().create_profile(
            session, UserProfileInput(name=name), make_default=make_default
        )
        await _audit_admin(session, "profile.created", "user_profile", str(profile.id))
        notice = "profile_created"
        if has_resume:
            assert resume_file is not None
            resume = await resume_service.upload(
                session,
                profile_id=profile.id,
                name=resume_name.strip(),
                category=resume_category.strip(),
                filename=resume_file.filename or "resume.pdf",
                mime_type=resume_file.content_type or "",
                data=data,
                make_default=True,
            )
            await _audit_admin(
                session,
                "resume.uploaded",
                "resume",
                str(resume.id),
                details={"mime_type": resume.mime_type, "sha256": resume.sha256},
            )
            notice = "profile_and_resume_created"
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        with contextlib.suppress(Exception):
            resume_service.abort_last_pending_upload()
        raise
    try:
        await session.commit()
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        outcome = await resume_service.resolve_last_upload_commit_outcome()
        if outcome != "committed":
            raise
    if resume is not None:
        resume_service.finalize_upload(resume)
    return RedirectResponse(
        f"/admin?view=settings&profile_id={profile.id}&notice={notice}", status_code=303
    )


@router.post("/admin/profiles/{profile_id}/default")
async def make_default_profile(
    profile_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    try:
        profile = await ProfileService().set_default_profile(session, profile_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="profile not found") from exc
    await _audit_admin(session, "profile.default_changed", "user_profile", str(profile.id))
    await session.commit()
    return RedirectResponse(
        f"/admin?view=settings&profile_id={profile.id}&notice=profile_default", status_code=303
    )


@router.post("/admin/preferences")
async def save_preferences(
    request: Request,
    allowed_categories: str = Form(""),
    auto_send_categories: str = Form(""),
    forbidden_categories: str = Form(""),
    allowed_cities: str = Form(""),
    minimum_daily_applications: int = Form(0, ge=0, le=100),
    maximum_daily_applications: int = Form(3, ge=0, le=100),
    minimum_auto_send_score: int = Form(85, ge=0, le=100),
    force_minimum_daily_applications: bool = Form(False),
    daily_application_rules_present: bool = Form(False),
    remote_allowed: bool = Form(False),
    consider_outside_primary_resume: bool = Form(False),
    willing_without_experience: bool = Form(False),
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    if daily_application_rules_present and minimum_daily_applications > maximum_daily_applications:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="minimum daily applications cannot exceed the maximum",
        )
    payload = JobPreferenceUpdateInput(
        allowed_categories=_items(allowed_categories),
        auto_send_categories=_items(auto_send_categories),
        forbidden_categories=_items(forbidden_categories),
        allowed_cities=_items(allowed_cities),
        maximum_daily_applications=maximum_daily_applications,
        minimum_auto_send_score=minimum_auto_send_score,
        remote_allowed=remote_allowed,
        consider_outside_primary_resume=consider_outside_primary_resume,
        willing_without_experience=willing_without_experience,
    )
    preferences = await ProfileService().update_preferences(session, payload, profile_id)
    if daily_application_rules_present:
        preferences.additional_rules = _daily_application_rules(
            preferences.additional_rules,
            minimum=minimum_daily_applications,
            force_minimum=force_minimum_daily_applications,
        )
    daily_rules = preferences.additional_rules or {}
    await _audit_admin(
        session,
        "preferences.updated",
        "job_preference",
        str(preferences.id),
        details={
            "auto_send_enabled": preferences.auto_send_enabled,
            "global_pause": preferences.global_pause,
            "minimum_daily_applications": daily_rules.get("minimum_daily_applications", 0),
            "maximum_daily_applications": preferences.maximum_daily_applications,
            "force_minimum_daily_applications": daily_rules.get(
                "force_minimum_daily_applications", False
            ),
        },
    )
    await session.commit()
    return RedirectResponse(
        f"/admin?view=settings&profile_id={preferences.profile_id}&notice=preferences_saved",
        status_code=303,
    )


@router.post("/admin/pause/{paused}")
async def set_pause(
    paused: bool,
    request: Request,
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    return_view: Literal["overview", "settings"] = Form("overview"),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    service = ProfileService()
    preferences = (
        await service.pause_auto_send(session, profile_id)
        if paused
        else await service.resume_auto_send(session, profile_id)
    )
    await _audit_admin(
        session,
        "auto_send.paused" if paused else "auto_send.resumed",
        "job_preference",
        str(preferences.id),
        decision="paused" if paused else "enabled_and_resumed",
        details={
            "auto_send_enabled": preferences.auto_send_enabled,
            "global_pause": preferences.global_pause,
        },
    )
    await session.commit()
    notice = "auto_send_paused" if paused else "auto_send_resumed"
    return RedirectResponse(
        f"/admin?view={return_view}&profile_id={preferences.profile_id}&notice={notice}",
        status_code=303,
    )


@router.post("/admin/resumes")
async def admin_upload_resume(
    request: Request,
    profile_id: UUID | None = Form(None),
    name: str = Form(...),
    category: str = Form(...),
    file: UploadFile = File(...),
    make_default: bool = Form(False),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    settings = get_settings()
    data = await file.read(settings.max_resume_bytes + 1)
    profile = await ProfileService().get_profile(session, profile_id)
    if profile is None:
        raise ValueError("profile is required before resume upload")
    resume_service = ResumeService(settings)
    try:
        resume = await resume_service.upload(
            session,
            profile_id=profile.id,
            name=name,
            category=category,
            filename=file.filename or "resume.pdf",
            mime_type=file.content_type or "",
            data=data,
            make_default=make_default,
        )
        await _audit_admin(
            session,
            "resume.uploaded",
            "resume",
            str(resume.id),
            details={"mime_type": resume.mime_type, "sha256": resume.sha256},
        )
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        with contextlib.suppress(Exception):
            resume_service.abort_last_pending_upload()
        raise
    try:
        await session.commit()
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        outcome = await resume_service.resolve_last_upload_commit_outcome()
        if outcome != "committed":
            raise
    resume_service.finalize_upload(resume)
    return RedirectResponse(
        f"/admin?view=settings&profile_id={profile.id}&notice=resume_uploaded", status_code=303
    )


@router.post("/admin/oauth/gmail/disconnect")
async def admin_disconnect_gmail(
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    was_connected = await GmailOAuthService(get_settings()).disconnect(session)
    await _audit_admin(
        session,
        "oauth.gmail.disconnected",
        "oauth_credential",
        "gmail",
        decision="disconnected" if was_connected else "already_disconnected",
        details={"pending_authorizations_invalidated": True, "remote_grant_revoked": False},
    )
    await session.commit()
    return RedirectResponse("/admin?view=settings&notice=google_disconnected", status_code=303)


@router.post("/admin/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    return_view: str = Form("history"),
    history_kind: str = Form("alerts"),
    profile_id: UUID | None = Form(None),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    alert = await session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    alert.acknowledged = True
    await _audit_admin(
        session,
        "alert.acknowledged",
        "alert",
        str(alert.id),
        decision="acknowledged",
    )
    await session.commit()
    if return_view == "accounts":
        return RedirectResponse("/admin/accounts", status_code=303)
    target_view = return_view if return_view in _VIEW_TITLES else "history"
    target = f"/admin?view={target_view}&notice=alert_acknowledged"
    if target_view == "history":
        valid_kinds = {"alerts", "audit", "all", "sent", "rejected", "jobs", "matches", "scans"}
        target += f"&history_kind={history_kind if history_kind in valid_kinds else 'alerts'}"
    if profile_id is not None:
        target += f"&profile_id={profile_id}"
    return RedirectResponse(target, status_code=303)


@router.post("/admin/resumes/{resume_id}/verify")
async def verify_resume(
    resume_id: UUID,
    request: Request,
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    resume = await session.get(Resume, resume_id)
    selected_profile = await ProfileService().get_profile(session, profile_id)
    if resume is None or selected_profile is None or resume.profile_id != selected_profile.id:
        raise HTTPException(status_code=404)
    resume.verified = True
    resume.active = True
    await _audit_admin(session, "resume.verified", "resume", str(resume.id))
    await session.commit()
    return RedirectResponse(
        f"/admin?view=settings&profile_id={selected_profile.id}&notice=resume_verified",
        status_code=303,
    )


async def _owned_resume(
    session: AsyncSession, resume_id: UUID, profile_id: UUID | None
) -> tuple[Resume, UserProfile]:
    resume = await session.get(Resume, resume_id)
    selected_profile = await ProfileService().get_profile(session, profile_id)
    if resume is None or selected_profile is None or resume.profile_id != selected_profile.id:
        raise HTTPException(status_code=404)
    return resume, selected_profile


@router.post("/admin/resumes/{resume_id}/deactivate")
async def admin_deactivate_resume(
    resume_id: UUID,
    request: Request,
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    _resume, selected_profile = await _owned_resume(session, resume_id, profile_id)
    await ResumeService(get_settings()).deactivate(session, resume_id)
    await _audit_admin(session, "resume.deactivated", "resume", str(resume_id))
    await session.commit()
    return RedirectResponse(
        f"/admin?view=settings&profile_id={selected_profile.id}&notice=resume_deactivated",
        status_code=303,
    )


@router.post("/admin/resumes/{resume_id}/activate")
async def admin_activate_resume(
    resume_id: UUID,
    request: Request,
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    _resume, selected_profile = await _owned_resume(session, resume_id, profile_id)
    try:
        await ResumeService(get_settings()).activate(session, resume_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit_admin(session, "resume.activated", "resume", str(resume_id))
    await session.commit()
    return RedirectResponse(
        f"/admin?view=settings&profile_id={selected_profile.id}&notice=resume_activated",
        status_code=303,
    )


@router.post("/admin/resumes/{resume_id}/archive")
async def admin_archive_resume(
    resume_id: UUID,
    request: Request,
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    _resume, selected_profile = await _owned_resume(session, resume_id, profile_id)
    await ResumeService(get_settings()).archive(session, resume_id)
    await _audit_admin(session, "resume.archived", "resume", str(resume_id))
    await session.commit()
    return RedirectResponse(
        f"/admin?view=settings&profile_id={selected_profile.id}&notice=resume_archived",
        status_code=303,
    )


@router.post("/admin/resumes/{resume_id}/restore")
async def admin_restore_resume(
    resume_id: UUID,
    request: Request,
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    _resume, selected_profile = await _owned_resume(session, resume_id, profile_id)
    await ResumeService(get_settings()).restore(session, resume_id)
    await _audit_admin(session, "resume.restored", "resume", str(resume_id))
    await session.commit()
    return RedirectResponse(
        f"/admin?view=settings&profile_id={selected_profile.id}&notice=resume_restored",
        status_code=303,
    )


@router.post("/admin/resumes/{resume_id}/delete")
async def admin_delete_resume(
    resume_id: UUID,
    request: Request,
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    _resume, selected_profile = await _owned_resume(session, resume_id, profile_id)
    settings = get_settings()
    resume_service = ResumeService(settings)
    deletion: ResumeDeletion | None = None
    try:
        deletion = await resume_service.delete(session, resume_id)
        await _audit_admin(
            session,
            "resume.deleted",
            "resume",
            str(resume_id),
            details=deletion.audit_details(),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResumeInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        with contextlib.suppress(Exception):
            if deletion is not None:
                resume_service.abort_pending_delete(deletion)
        raise
    try:
        await session.commit()
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        outcome = "unknown"
        if deletion is not None:
            outcome = await resume_service.resolve_delete_commit_outcome(deletion)
        if outcome != "committed":
            raise
    resume_service.finalize_delete(deletion)
    return RedirectResponse(
        f"/admin?view=settings&profile_id={selected_profile.id}&notice=resume_deleted",
        status_code=303,
    )


@router.get("/admin/resumes/{resume_id}/file")
async def admin_resume_file(
    resume_id: UUID,
    request: Request,
    profile_id: UUID | None = None,
    _: str = Depends(require_admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    resume = await session.get(Resume, resume_id)
    selected_profile = await ProfileService().get_profile(session, profile_id)
    if resume is None or selected_profile is None or resume.profile_id != selected_profile.id:
        raise HTTPException(status_code=404)
    if resume.storage_key.startswith("pending/"):
        raise HTTPException(status_code=404, detail="resume file has not been uploaded")
    settings = get_settings()
    try:
        data = read_verified_resume(
            settings.resume_storage_path,
            resume.storage_key,
            expected_sha256=resume.sha256,
            expected_mime_type=resume.mime_type,
            max_bytes=settings.max_resume_bytes,
        )
    except UnsafeResumeError as exc:
        raise HTTPException(status_code=404, detail="resume file is unavailable") from exc
    filename = quote(resume.original_filename)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{filename}",
            "Cache-Control": "no-store",
        },
    )


@router.post("/admin/profile-sources/{source_id}/selection")
async def admin_select_profile_source(
    source_id: UUID,
    request: Request,
    profile_id: UUID = Form(...),
    enabled: bool = Form(...),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    try:
        await set_source_selected(
            session, profile_id=profile_id, source_id=source_id, enabled=enabled
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="profile or source not found") from exc
    await _audit_admin(
        session,
        "profile_source.selected" if enabled else "profile_source.deselected",
        "user_profile",
        str(profile_id),
        details={"source_id": str(source_id)},
    )
    await session.commit()
    return RedirectResponse(
        f"/admin?view=settings&profile_id={profile_id}&notice=source_selection_saved",
        status_code=303,
    )


@router.post("/admin/sources/{source_id}/toggle")
async def toggle_source(
    source_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    source = await session.get(JobSource, source_id)
    if source is None:
        raise HTTPException(status_code=404)
    enabling = not source.enabled
    try:
        if enabling:
            enable_source_record(source)
        else:
            disable_source_record(source)
    except SourceControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _audit_admin(
        session,
        "source.enabled" if enabling else "source.disabled",
        "job_source",
        str(source.id),
        decision="enabled" if enabling else "disabled",
    )
    await session.commit()
    notice = "source_enabled" if enabling else "source_disabled"
    return RedirectResponse(f"/admin?view=settings&notice={notice}", status_code=303)


@router.post("/admin/sources/{source_id}/scan/{scan_type}")
async def admin_start_scan(
    source_id: UUID,
    scan_type: ScanType,
    request: Request,
    csrf_token: str = Form(...),
    profile_id: UUID | None = Form(None),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    from app.database.session import async_session_factory
    from app.scheduler.tasks import run_scan_task

    run = await ScanService(async_session_factory, build_default_registry()).create_scan(
        source_id, scan_type, actor="admin"
    )
    try:
        run_scan_task.delay(str(run.id))
    except Exception as exc:
        async with async_session_factory() as session:
            stored = await session.get(ScanRun, run.id)
            if stored is not None:
                stored.status = RunStatus.FAILED
                stored.diagnostics = {"queue_error": type(exc).__name__}
                await session.commit()
        raise HTTPException(status_code=503, detail="task queue unavailable") from exc
    target = "/admin?view=settings&notice=scan_started"
    if profile_id is not None:
        target += f"&profile_id={profile_id}"
    return RedirectResponse(f"{target}#source-{source_id}", status_code=303)


@router.get("/admin/applications/{application_id}", response_class=HTMLResponse)
async def admin_application_detail(
    application_id: UUID,
    request: Request,
    notice: str | None = None,
    _: str = Depends(require_admin_page),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        detail = await get_application_detail(session, application_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="application not found") from exc
    from app.ui.dashboard import render_dashboard

    application = await session.get(Application, application_id)
    assert application is not None
    return await render_dashboard(
        request,
        session,
        get_settings(),
        _session_token(request),
        profile_id=application.profile_id,
        view="decisions",
        notice=notice,
        template_name="application_detail.html",
        extra_context={"application": detail, "view_title": "Проверка отклика"},
    )


@router.post("/admin/applications/{application_id}/approve")
async def admin_approve_application(
    application_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    return_to: str = Form("detail"),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    try:
        application = await ApplicationService(get_settings()).approve(session, application_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="application not found") from exc
    except ApplicationPreparationError as exc:
        failed_application = await session.get(Application, application_id)
        notice = _approval_failure_notice(failed_application, exc)
        if return_to == "decisions" and failed_application is not None:
            return RedirectResponse(
                f"/admin?view=decisions&profile_id={failed_application.profile_id}&notice={notice}",
                status_code=303,
            )
        return RedirectResponse(
            f"/admin/applications/{application_id}?notice={notice}", status_code=303
        )
    feedback = await ReviewLearningService().record_decision(
        session,
        application,
        outcome=ReviewOutcome.APPROVED,
        actor="admin",
    )
    await _audit_admin(
        session,
        "application.approved",
        "application",
        str(application.id),
        decision=application.status.value,
        details={"learning_eligible": feedback.learning_eligible},
    )
    await session.commit()
    if return_to == "decisions":
        return RedirectResponse(
            f"/admin?view=decisions&profile_id={application.profile_id}&notice=application_approved",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/applications/{application_id}?notice=application_approved", status_code=303
    )


@router.post("/admin/applications/{application_id}/reject")
async def admin_reject_application(
    application_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    reason: str = Form(""),
    reason_code: str = Form(ReviewReason.OTHER.value),
    learn_from_review: bool = Form(True),
    return_to: str = Form("detail"),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    try:
        structured_reason = ReviewReason(reason_code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid review reason") from exc
    try:
        application = await ApplicationService(get_settings()).reject(session, application_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="application not found") from exc
    except ApplicationPreparationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    clean_reason = reason.strip()[:500]
    feedback = await ReviewLearningService().record_decision(
        session,
        application,
        outcome=ReviewOutcome.REJECTED,
        actor="admin",
        reason=structured_reason,
        reason_text=clean_reason,
        learn=learn_from_review,
    )
    await _audit_admin(
        session,
        "application.rejected_by_owner",
        "application",
        str(application.id),
        decision=application.status.value,
        details={
            "reason_code": structured_reason.value,
            "reason": clean_reason,
            "learning_eligible": feedback.learning_eligible,
        },
    )
    await session.commit()
    if return_to == "decisions":
        return RedirectResponse(
            f"/admin?view=decisions&profile_id={application.profile_id}&notice=application_rejected",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/applications/{application_id}?notice=application_rejected", status_code=303
    )


@router.post("/admin/review-learning/influence")
async def admin_set_review_learning_influence(
    request: Request,
    profile_id: UUID = Form(...),
    enabled: bool = Form(...),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    profile = await ProfileService().get_profile(session, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    preferences = await ProfileService().get_preferences(session, profile.id)
    await ReviewLearningService().set_influence(
        session,
        profile.id,
        enabled=enabled,
        ignored_dimensions=fixed_preference_dimensions(preferences.allowed_cities),
    )
    await _audit_admin(
        session,
        "review_learning.influence_changed",
        "profile",
        str(profile.id),
        decision="enabled" if enabled else "paused",
    )
    await session.commit()
    notice = "review_learning_enabled" if enabled else "review_learning_paused"
    return RedirectResponse(
        f"/admin?view=decisions&profile_id={profile.id}&notice={notice}", status_code=303
    )


@router.post("/admin/applications/{application_id}/send")
async def admin_send_application(
    application_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    from app.database.session import async_session_factory

    try:
        delivery = await EmailService(get_settings(), async_session_factory).send_application(
            application_id
        )
    except LookupError as exc:
        await _audit_admin(
            session,
            "application.send_rejected",
            "application",
            str(application_id),
            decision="not_found",
        )
        await session.commit()
        raise HTTPException(status_code=404, detail="application not found") from exc
    except EmailSendBlocked as exc:
        await _audit_admin(
            session,
            "application.send_rejected",
            "application",
            str(application_id),
            decision="blocked",
            details={"error_type": type(exc).__name__},
        )
        await session.commit()
        raise HTTPException(
            status_code=409, detail="application is not eligible for delivery"
        ) from exc
    await _audit_admin(
        session,
        "application.send_requested",
        "application",
        str(application_id),
        decision=delivery.status.value,
    )
    await session.commit()
    return RedirectResponse(
        f"/admin/applications/{application_id}?notice=application_sent", status_code=303
    )


@router.post("/admin/applications/{application_id}/reconcile-delivery-unknown")
async def admin_reconcile_application_delivery(
    application_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    try:
        await reconcile_stale_delivery_unknown(session, application_id, actor="admin")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="application not found") from exc
    except DeliveryReconciliationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return RedirectResponse(
        f"/admin/applications/{application_id}?notice=delivery_reconciled", status_code=303
    )
