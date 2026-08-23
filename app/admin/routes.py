from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

# FastAPI's declarative dependency/form parameters intentionally call Depends/File.
# ruff: noqa: B008
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications import (
    ApplicationService,
    DeliveryReconciliationError,
    get_application_detail,
    reconcile_stale_delivery_unknown,
)
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
from app.matching.providers import MATCHING_RULES_VERSION
from app.models.entities import (
    Alert,
    Application,
    AuditEvent,
    JobSource,
    MatchEvaluation,
    Resume,
    ScanRun,
    SourceJob,
)
from app.models.enums import (
    ApplicationStatus,
    JobStatus,
    MatchDecision,
    RunStatus,
    ScanType,
    SourceHealth,
)
from app.profiles import ProfileService, ResumeService
from app.profiles.schemas import JobPreferenceUpdateInput, UserProfileInput
from app.security.auth import CsrfProtector, SessionSigner, verify_password
from app.security.ssrf import public_url_shape_is_safe
from app.settings import get_settings

router = APIRouter(tags=["admin"])
templates = Jinja2Templates(directory="app/admin/templates")


def _safe_external_link(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return None
    if not hostname or not public_url_shape_is_safe(value, (hostname,)):
        return None
    return value


templates.env.globals["safe_external_link"] = _safe_external_link


_LOCAL_TZ = ZoneInfo("Europe/Chisinau")
_STATUS_LABELS = {
    "healthy": "Работает",
    "degraded": "Есть проблемы",
    "paused": "На паузе",
    "disabled": "Выключен",
    "unknown": "Неизвестно",
    "queued": "В очереди",
    "running": "Выполняется",
    "succeeded": "Успешно",
    "partial": "Частично",
    "failed": "Ошибка",
    "cancelled": "Отменено",
    "active": "Активна",
    "possibly_closed": "Возможно закрыта",
    "closed": "Закрыта",
    "incomplete": "Неполная",
    "auto_apply": "AUTO_APPLY",
    "prepare_for_review": "REVIEW",
    "skip": "SKIP",
    "block": "BLOCK",
    "prepared": "Подготовлен",
    "skipped": "Пропущен",
    "pending_review": "На проверке",
    "approved": "Одобрен",
    "auto_approved": "Auto-approved",
    "sending": "Отправляется",
    "sent": "Отправлен",
    "delivery_unknown": "Доставка неизвестна",
    "temporary_failure": "Временная ошибка",
    "permanent_failure": "Ошибка доставки",
    "blocked": "Заблокирован",
    "incremental": "Инкрементальный",
    "full": "Полный",
    "recheck": "Перепроверка",
}


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "unknown")


def _status_label(value: Any) -> str:
    raw = _enum_value(value)
    return _STATUS_LABELS.get(raw, raw.replace("_", " ").capitalize())


def _status_tone(value: Any) -> str:
    raw = _enum_value(value)
    if raw in {"healthy", "succeeded", "active", "auto_apply", "approved", "auto_approved", "sent"}:
        return "success"
    if raw in {
        "queued",
        "running",
        "partial",
        "pending_review",
        "prepared",
        "sending",
        "possibly_closed",
        "unknown",
        "prepare_for_review",
        "temporary_failure",
    }:
        return "warning"
    if raw in {"degraded", "failed", "blocked", "block", "permanent_failure", "delivery_unknown"}:
        return "danger"
    if raw in {"disabled", "cancelled", "closed", "skip", "skipped"}:
        return "muted"
    return "info"


def _format_dt(value: datetime | None, include_date: bool = True) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone(_LOCAL_TZ)
    return local.strftime("%d.%m.%Y %H:%M" if include_date else "%H:%M")


templates.env.globals["status_label"] = _status_label
templates.env.globals["status_tone"] = _status_tone
templates.env.globals["format_dt"] = _format_dt


def _signer() -> SessionSigner:
    return SessionSigner(get_settings().secret_key.get_secret_value())


def _csrf() -> CsrfProtector:
    return CsrfProtector(get_settings().secret_key.get_secret_value())


def _session_token(request: Request) -> str:
    token = request.cookies.get(get_settings().session_cookie_name)
    subject = _signer().verify(token, get_settings().session_ttl_seconds) if token else None
    if subject != get_settings().admin_username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
    assert token is not None
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


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, oauth_error: str | None = None) -> HTMLResponse:
    settings = get_settings()
    google_oauth = GmailOAuthService(settings)
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "csrf_token": _csrf().issue("login"),
            "error": (
                "Вход через Google не завершён. Проверьте выбранный аккаунт и повторите."
                if oauth_error
                else None
            ),
            "google_login_available": (
                google_oauth.configured and bool(settings.google_admin_emails)
            ),
            "password_login_available": settings.admin_password_hash is not None,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/admin/auth/google")
async def google_admin_login_start(
    consent: bool = False,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    settings = get_settings()
    service = GmailOAuthService(settings)
    if not service.configured or not settings.google_admin_emails:
        return RedirectResponse("/login?oauth_error=configuration", status_code=303)
    try:
        oauth_status = await service.get_status(session)
        authorization = await service.create_authorization_request(
            session,
            actor=GOOGLE_ADMIN_OAUTH_ACTOR,
            force_consent=consent or not oauth_status["connected"],
        )
    except GmailOAuthError as exc:
        await session.rollback()
        await record_audit_event(
            session,
            actor="google-login",
            action="admin.login.google_start_failed",
            entity_type="admin_session",
            entity_id="google",
            correlation_id=str(exc.correlation_id or "google"),
            decision="failed",
            details={"provider": "google", "error_code": exc.code},
        )
        await session.commit()
        return RedirectResponse("/login?oauth_error=start", status_code=303)

    await record_audit_event(
        session,
        actor="google-login",
        action="admin.login.google_started",
        entity_type="oauth_authorization_request",
        entity_id=str(authorization.request_id),
        correlation_id=str(authorization.request_id),
        decision="redirected",
        details={"provider": "google", "expires_at": authorization.expires_at.isoformat()},
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


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
) -> Response:
    settings = get_settings()
    csrf_valid = _csrf().verify(csrf_token, "login", settings.csrf_ttl_seconds)
    password_valid = bool(
        settings.admin_password_hash
        and verify_password(password, settings.admin_password_hash.get_secret_value())
    )
    if not csrf_valid or username != settings.admin_username or not password_valid:
        google_oauth = GmailOAuthService(settings)
        error_response = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "csrf_token": _csrf().issue("login"),
                "error": "Неверные данные",
                "google_login_available": (
                    google_oauth.configured and bool(settings.google_admin_emails)
                ),
                "password_login_available": settings.admin_password_hash is not None,
            },
            status_code=401,
        )
        error_response.headers["Cache-Control"] = "no-store"
        return error_response
    token = _signer().issue(username)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_seconds,
        secure=settings.public_base_url.casefold().startswith("https://"),
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    require_csrf(request, csrf_token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return response


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    profile_id: UUID | None = None,
    _: str = Depends(require_admin_page),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    token = _session_token(request)
    profile_service = ProfileService()
    profiles = await profile_service.list_profiles(session)
    profile = await profile_service.get_profile(session, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    selected_profile_id = profile.id
    preferences = await profile_service.get_preferences(session, selected_profile_id)
    sources = list((await session.scalars(select(JobSource).order_by(JobSource.name))).all())
    resumes = list(
        (
            await session.scalars(
                select(Resume)
                .where(Resume.profile_id == selected_profile_id)
                .order_by(desc(Resume.created_at))
            )
        ).all()
    )
    jobs = list(
        (
            await session.scalars(
                select(SourceJob).order_by(desc(SourceJob.last_seen_at)).limit(50)
            )
        ).all()
    )
    matches = list(
        (
            await session.scalars(
                select(MatchEvaluation)
                .where(MatchEvaluation.profile_id == selected_profile_id)
                .order_by(desc(MatchEvaluation.created_at))
                .limit(30)
            )
        ).all()
    )
    applications = list(
        (
            await session.scalars(
                select(Application)
                .where(Application.profile_id == selected_profile_id)
                .order_by(desc(Application.created_at))
                .limit(40)
            )
        ).all()
    )
    scans = list(
        (await session.scalars(select(ScanRun).order_by(desc(ScanRun.started_at)).limit(30))).all()
    )
    alerts = list(
        (await session.scalars(select(Alert).order_by(desc(Alert.created_at)).limit(30))).all()
    )
    audits = list(
        (
            await session.scalars(select(AuditEvent).order_by(desc(AuditEvent.timestamp)).limit(40))
        ).all()
    )

    match_job_ids = {item.source_job_id for item in matches}
    match_jobs = {}
    if match_job_ids:
        match_jobs = {
            item.id: item
            for item in (
                await session.scalars(select(SourceJob).where(SourceJob.id.in_(match_job_ids)))
            ).all()
        }
    application_job_ids = {item.source_job_id for item in applications}
    application_jobs = {}
    if application_job_ids:
        application_jobs = {
            item.id: item
            for item in (
                await session.scalars(
                    select(SourceJob).where(SourceJob.id.in_(application_job_ids))
                )
            ).all()
        }

    now_local = datetime.now(_LOCAL_TZ)
    start_local = datetime.combine(now_local.date(), time.min, _LOCAL_TZ)
    start = start_local.astimezone(UTC)
    end = (start_local + timedelta(days=1)).astimezone(UTC)
    today_scans = list(
        (
            await session.scalars(
                select(ScanRun).where(ScanRun.started_at >= start, ScanRun.started_at < end)
            )
        ).all()
    )
    decision_rows = (
        await session.execute(
            select(MatchEvaluation.decision, func.count(MatchEvaluation.id))
            .where(
                MatchEvaluation.profile_id == selected_profile_id,
                MatchEvaluation.created_at >= start,
                MatchEvaluation.created_at < end,
            )
            .group_by(MatchEvaluation.decision)
        )
    ).all()
    decisions = {decision: int(count) for decision, count in decision_rows}
    current_match_exists = (
        select(MatchEvaluation.id)
        .where(
            MatchEvaluation.source_job_id == SourceJob.id,
            MatchEvaluation.profile_id == selected_profile_id,
            MatchEvaluation.prompt_rules_version == MATCHING_RULES_VERSION,
            MatchEvaluation.source_content_hash == SourceJob.content_hash,
        )
        .correlate(SourceJob)
        .exists()
    )

    counts = {
        "jobs": int(await session.scalar(select(func.count(SourceJob.id))) or 0),
        "active_jobs": int(
            await session.scalar(
                select(func.count(SourceJob.id)).where(SourceJob.status == JobStatus.ACTIVE)
            )
            or 0
        ),
        "applications": int(
            await session.scalar(
                select(func.count(Application.id)).where(
                    Application.profile_id == selected_profile_id
                )
            )
            or 0
        ),
        "pending_review": int(
            await session.scalar(
                select(func.count(Application.id)).where(
                    Application.profile_id == selected_profile_id,
                    Application.status == ApplicationStatus.PENDING_REVIEW,
                )
            )
            or 0
        ),
        "running_scans": int(
            await session.scalar(
                select(func.count(ScanRun.id)).where(
                    ScanRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING])
                )
            )
            or 0
        ),
        "enabled_sources": sum(1 for item in sources if item.enabled),
        "healthy_sources": sum(
            1 for item in sources if item.enabled and item.health_status == SourceHealth.HEALTHY
        ),
        "unacknowledged_alerts": int(
            await session.scalar(select(func.count(Alert.id)).where(Alert.acknowledged.is_(False)))
            or 0
        ),
    }
    matching_backlog = int(
        await session.scalar(
            select(func.count(SourceJob.id)).where(
                SourceJob.status == JobStatus.ACTIVE,
                SourceJob.canonical_job_id.is_not(None),
                ~current_match_exists,
            )
        )
        or 0
    )
    sent_today = int(
        await session.scalar(
            select(func.count(Application.id)).where(
                Application.profile_id == selected_profile_id,
                Application.sent_at >= start,
                Application.sent_at < end,
            )
        )
        or 0
    )
    overview = {
        "today_found": sum(item.found_jobs for item in today_scans),
        "today_new": sum(item.new_jobs for item in today_scans),
        "today_updated": sum(item.updated_jobs for item in today_scans),
        "today_scan_errors": sum(item.parsing_errors + item.network_errors for item in today_scans),
        "today_matches": sum(decisions.values()),
        "auto_apply": decisions.get(MatchDecision.AUTO_APPLY, 0),
        "review": decisions.get(MatchDecision.PREPARE_FOR_REVIEW, 0),
        "skip": decisions.get(MatchDecision.SKIP, 0),
        "block": decisions.get(MatchDecision.BLOCK, 0),
        "sent_today": sent_today,
        "daily_limit": preferences.maximum_daily_applications,
        "matching_backlog": matching_backlog,
        "rules_version": MATCHING_RULES_VERSION,
    }
    gmail_oauth = await GmailOAuthService(get_settings()).get_status(session)
    attention_items: list[dict[str, str]] = []
    if not gmail_oauth["configured"]:
        attention_items.append(
            {
                "tone": "danger",
                "title": "Google OAuth не настроен",
                "detail": "Вход через Google и автономная отправка Gmail недоступны.",
                "href": "#health",
                "action": "Открыть систему",
            }
        )
    elif not gmail_oauth["connected"]:
        attention_items.append(
            {
                "tone": "danger",
                "title": "Google-аккаунт не подключён",
                "detail": "Письма не смогут отправляться до повторного входа через Google.",
                "href": "/admin/auth/google",
                "action": "Подключить",
            }
        )
    elif not gmail_oauth["identity_verified"]:
        attention_items.append(
            {
                "tone": "warning",
                "title": "Подтвердите Google identity",
                "detail": "Gmail token есть, но вход через разрешённый аккаунт ещё не завершён.",
                "href": "/admin/auth/google",
                "action": "Войти через Google",
            }
        )
    if counts["unacknowledged_alerts"]:
        attention_items.append(
            {
                "tone": "danger",
                "title": f"{counts['unacknowledged_alerts']} системных предупреждений",
                "detail": "Проверьте свежие alerts и подтвердите просмотр.",
                "href": "#health",
                "action": "Проверить",
            }
        )
    unhealthy_sources = counts["enabled_sources"] - counts["healthy_sources"]
    if unhealthy_sources:
        attention_items.append(
            {
                "tone": "danger",
                "title": f"{unhealthy_sources} источников требуют проверки",
                "detail": "Один или несколько включённых crawler не находятся в healthy.",
                "href": "#sources",
                "action": "Источники",
            }
        )
    if counts["pending_review"]:
        attention_items.append(
            {
                "tone": "warning",
                "title": f"{counts['pending_review']} откликов ждут решения",
                "detail": "Нейросеть подготовила их, но финальное действие остаётся за вами.",
                "href": "#applications",
                "action": "Открыть очередь",
            }
        )
    if preferences.global_pause:
        attention_items.append(
            {
                "tone": "warning",
                "title": "Автоотправка на паузе",
                "detail": "Автоматизация продолжит анализ, но не отправит новые отклики.",
                "href": "#preferences",
                "action": "Управление",
            }
        )
    attention_tone = (
        "danger"
        if any(item["tone"] == "danger" for item in attention_items)
        else "warning"
        if attention_items
        else "success"
    )
    source_names = {item.id: item.name for item in sources}

    response = templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "csrf_token": _csrf().issue(token),
            "profile": profile,
            "profiles": profiles,
            "selected_profile_id": selected_profile_id,
            "preferences": preferences,
            "sources": sources,
            "source_names": source_names,
            "resumes": resumes,
            "jobs": jobs,
            "matches": matches,
            "match_jobs": match_jobs,
            "applications": applications,
            "application_jobs": application_jobs,
            "scans": scans,
            "alerts": alerts,
            "audits": audits,
            "counts": counts,
            "overview": overview,
            "gmail_oauth": gmail_oauth,
            "attention_items": attention_items,
            "attention_tone": attention_tone,
            "now_local": now_local,
            "settings": get_settings(),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


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
    return RedirectResponse(f"/?profile_id={profile.id}#profile", status_code=303)


@router.post("/admin/profiles")
async def create_profile(
    request: Request,
    name: str = Form(...),
    make_default: bool = Form(False),
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    profile = await ProfileService().create_profile(
        session, UserProfileInput(name=name), make_default=make_default
    )
    await _audit_admin(session, "profile.created", "user_profile", str(profile.id))
    await session.commit()
    return RedirectResponse(f"/?profile_id={profile.id}#profile", status_code=303)


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
    return RedirectResponse(f"/?profile_id={profile.id}#profile", status_code=303)


@router.post("/admin/preferences")
async def save_preferences(
    request: Request,
    allowed_categories: str = Form(""),
    auto_send_categories: str = Form(""),
    forbidden_categories: str = Form(""),
    allowed_cities: str = Form(""),
    maximum_daily_applications: int = Form(3),
    minimum_auto_send_score: int = Form(85),
    remote_allowed: bool = Form(False),
    consider_outside_primary_resume: bool = Form(False),
    willing_without_experience: bool = Form(False),
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
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
    await _audit_admin(
        session,
        "preferences.updated",
        "job_preference",
        str(preferences.id),
        details={
            "auto_send_enabled": preferences.auto_send_enabled,
            "global_pause": preferences.global_pause,
        },
    )
    await session.commit()
    return RedirectResponse(f"/?profile_id={preferences.profile_id}#preferences", status_code=303)


@router.post("/admin/pause/{paused}")
async def set_pause(
    paused: bool,
    request: Request,
    profile_id: UUID | None = Form(None),
    csrf_token: str = Form(...),
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
    return RedirectResponse(f"/?profile_id={preferences.profile_id}#preferences", status_code=303)


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
    resume = await ResumeService(settings).upload(
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
    await session.commit()
    return RedirectResponse(f"/?profile_id={profile.id}#resumes", status_code=303)


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
    return RedirectResponse("/#health", status_code=303)


@router.post("/admin/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
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
    return RedirectResponse("/#health", status_code=303)


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
    return RedirectResponse(f"/?profile_id={selected_profile.id}#resumes", status_code=303)


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
    return RedirectResponse("/#sources", status_code=303)


@router.post("/admin/sources/{source_id}/scan/{scan_type}")
async def admin_start_scan(
    source_id: UUID,
    scan_type: ScanType,
    request: Request,
    csrf_token: str = Form(...),
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
    return RedirectResponse("/#scans", status_code=303)


@router.get("/admin/applications/{application_id}", response_class=HTMLResponse)
async def admin_application_detail(
    application_id: UUID,
    request: Request,
    _: str = Depends(require_admin_page),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        detail = await get_application_detail(session, application_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="application not found") from exc
    token = _session_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="application_detail.html",
        context={"application": detail, "csrf_token": _csrf().issue(token)},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/admin/applications/{application_id}/approve")
async def admin_approve_application(
    application_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_csrf(request, csrf_token)
    application = await ApplicationService(get_settings()).approve(session, application_id)
    await _audit_admin(
        session,
        "application.approved",
        "application",
        str(application.id),
        decision=application.status.value,
    )
    await session.commit()
    return RedirectResponse(f"/admin/applications/{application_id}", status_code=303)


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
    return RedirectResponse(f"/admin/applications/{application_id}", status_code=303)


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
    return RedirectResponse(f"/admin/applications/{application_id}", status_code=303)
