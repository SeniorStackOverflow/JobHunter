from __future__ import annotations

# FastAPI's declarative dependency/form parameters intentionally call Depends/File.
# ruff: noqa: B008
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import require_api_actor
from app.api.schemas import ApplicationPrepareInput, ScanStartResponse, SourceInput, SourceUpdate
from app.applications import (
    DeliveryReconciliationError,
    get_application_detail,
    reconcile_stale_delivery_unknown,
)
from app.applications.service import ApplicationPreparationError, ApplicationService
from app.audit import record_audit_event
from app.crawlers.lifecycle import managed_adapter
from app.crawlers.registry import build_default_registry
from app.crawlers.source_control import disable_source_record, enable_source_record
from app.database import get_session
from app.email.oauth import GmailOAuthService
from app.email.service import EmailSendBlocked, EmailService
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
from app.models.enums import RunStatus, ScanType, SourceHealth
from app.profiles import ProfileService, ResumeService
from app.profiles.schemas import JobPreferenceUpdateInput, UserProfileInput
from app.security.auth import SessionSigner
from app.settings import get_settings

router = APIRouter(prefix="/api/v1", tags=["api"])


def public_model(obj: Any, *fields: str) -> dict[str, Any]:
    return {field: getattr(obj, field) for field in fields}


async def _audit_send_request(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    actor: str,
    application_id: UUID,
    decision: str,
    error_type: str | None = None,
) -> None:
    async with session_factory() as session:
        await record_audit_event(
            session,
            actor=actor,
            action=(
                "application.send_requested" if error_type is None else "application.send_rejected"
            ),
            entity_type="application",
            entity_id=str(application_id),
            correlation_id=str(application_id),
            decision=decision,
            details={"error_type": error_type} if error_type else None,
        )
        await session.commit()


async def _validate_source_configuration(source: JobSource) -> None:
    """Reject malformed adapter configuration before it can be persisted."""
    registry = build_default_registry()
    try:
        async with managed_adapter(registry.create(source)) as adapter:
            validation = await adapter.validate_source()
    except (RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": "invalid source configuration", "error_type": type(exc).__name__},
        ) from exc
    if not validation.valid:
        raise HTTPException(
            status_code=422,
            detail={"message": "invalid source configuration", "errors": validation.errors},
        )


@router.get("/status", dependencies=[Depends(require_api_actor)])
async def system_status(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    sources = list((await session.scalars(select(JobSource))).all())
    running = len(
        list(
            (
                await session.scalars(select(ScanRun.id).where(ScanRun.status == RunStatus.RUNNING))
            ).all()
        )
    )
    return {
        "sources": len(sources),
        "healthy_sources": sum(item.health_status == SourceHealth.HEALTHY for item in sources),
        "running_scans": running,
        "real_email_delivery_enabled": get_settings().real_email_delivery_enabled,
        "emergency_email_kill_switch": get_settings().emergency_email_kill_switch,
    }


@router.get("/profile", dependencies=[Depends(require_api_actor)])
async def get_profile(session: AsyncSession = Depends(get_session)) -> dict[str, Any] | None:
    profile = await ProfileService().get_profile(session)
    if profile is None:
        return None
    return public_model(
        profile,
        "id",
        "name",
        "contact_email",
        "phone",
        "location",
        "languages",
        "work_experience",
        "education",
        "skills",
        "driving_licences",
        "confirmed_facts",
        "availability",
        "updated_at",
    )


@router.put("/profile")
async def update_profile(
    payload: UserProfileInput,
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    profile = await ProfileService().upsert_profile(session, payload)
    await record_audit_event(
        session,
        actor=actor,
        action="profile.updated",
        entity_type="user_profile",
        entity_id=str(profile.id),
        correlation_id=str(profile.id),
    )
    await session.commit()
    return {"id": profile.id, "updated_at": profile.updated_at}


@router.get("/preferences", dependencies=[Depends(require_api_actor)])
async def get_preferences(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    item = await ProfileService().get_preferences(session)
    return public_model(
        item,
        "id",
        "allowed_categories",
        "auto_send_categories",
        "forbidden_categories",
        "allowed_cities",
        "remote_allowed",
        "minimum_salary",
        "salary_currency",
        "allowed_schedules",
        "forbidden_schedules",
        "willing_without_experience",
        "consider_outside_primary_resume",
        "language_constraints",
        "maximum_daily_applications",
        "minimum_auto_send_score",
        "additional_rules",
        "auto_send_enabled",
        "global_pause",
    )


@router.put("/preferences")
async def update_preferences(
    payload: JobPreferenceUpdateInput,
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    item = await ProfileService().update_preferences(session, payload)
    await record_audit_event(
        session,
        actor=actor,
        action="preferences.updated",
        entity_type="job_preference",
        entity_id=str(item.id),
        correlation_id=str(item.id),
    )
    await session.commit()
    return {"id": item.id, "updated_at": item.updated_at}


async def _set_auto_send_state(
    session: AsyncSession, *, actor: str, paused: bool
) -> dict[str, Any]:
    service = ProfileService()
    item = (
        await service.pause_auto_send(session)
        if paused
        else await service.resume_auto_send(session)
    )
    await record_audit_event(
        session,
        actor=actor,
        action="auto_send.paused" if paused else "auto_send.resumed",
        entity_type="job_preference",
        entity_id=str(item.id),
        correlation_id=str(item.id),
        decision="paused" if paused else "enabled_and_resumed",
        details={
            "auto_send_enabled": item.auto_send_enabled,
            "global_pause": item.global_pause,
        },
    )
    await session.commit()
    return {
        "auto_send_enabled": item.auto_send_enabled,
        "global_pause": item.global_pause,
    }


@router.post("/preferences/pause")
async def pause_auto_send(
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _set_auto_send_state(session, actor=actor, paused=True)


@router.post("/preferences/resume")
async def resume_auto_send(
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _set_auto_send_state(session, actor=actor, paused=False)


@router.get("/resumes", dependencies=[Depends(require_api_actor)])
async def list_resumes(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    values = list((await session.scalars(select(Resume).order_by(desc(Resume.created_at)))).all())
    return [
        public_model(
            item,
            "id",
            "name",
            "category",
            "original_filename",
            "mime_type",
            "sha256",
            "active",
            "verified",
            "is_default",
            "created_at",
        )
        for item in values
    ]


@router.post("/resumes")
async def upload_resume(
    name: str = Form(...),
    category: str = Form(...),
    file: UploadFile = File(...),
    make_default: bool = Form(False),
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    settings = get_settings()
    data = await file.read(settings.max_resume_bytes + 1)
    profile = await ProfileService().get_profile(session)
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
    await record_audit_event(
        session,
        actor=actor,
        action="resume.uploaded",
        entity_type="resume",
        entity_id=str(resume.id),
        correlation_id=str(resume.id),
        details={"mime_type": resume.mime_type, "sha256": resume.sha256},
    )
    await session.commit()
    return {"id": resume.id, "sha256": resume.sha256, "verified": resume.verified}


@router.get("/sources", dependencies=[Depends(require_api_actor)])
async def list_sources(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    sources = list((await session.scalars(select(JobSource).order_by(JobSource.name))).all())
    return [
        public_model(
            item,
            "id",
            "name",
            "base_url",
            "adapter_type",
            "enabled",
            "rate_limit",
            "concurrency",
            "health_status",
            "last_scan_status",
            "automatic_actions_paused",
        )
        for item in sources
    ]


@router.post("/sources", dependencies=[Depends(require_api_actor)])
async def add_source(
    payload: SourceInput,
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if payload.adapter_type not in build_default_registry().list_available():
        raise HTTPException(status_code=422, detail="unknown adapter type")
    source = JobSource(
        name=payload.name,
        base_url=str(payload.base_url),
        adapter_type=payload.adapter_type,
        configuration=payload.configuration,
        enabled=payload.enabled,
        rate_limit=payload.rate_limit,
        concurrency=payload.concurrency,
        health_status=SourceHealth.UNKNOWN,
    )
    await _validate_source_configuration(source)
    if payload.enabled:
        enable_source_record(source)
    else:
        disable_source_record(source)
    session.add(source)
    await session.flush()
    await record_audit_event(
        session,
        actor=actor,
        action="source.created",
        entity_type="job_source",
        entity_id=str(source.id),
        correlation_id=str(source.id),
        details={"adapter_type": source.adapter_type, "base_url": source.base_url},
    )
    await session.commit()
    return {"id": source.id}


@router.patch("/sources/{source_id}")
async def update_source(
    source_id: UUID,
    payload: SourceUpdate,
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    source = await session.get(JobSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(source, key, str(value) if key == "base_url" else value)
    await _validate_source_configuration(source)
    source.health_status = SourceHealth.UNKNOWN
    await record_audit_event(
        session,
        actor=actor,
        action="source.updated",
        entity_type="job_source",
        entity_id=str(source.id),
        correlation_id=str(source.id),
        details={"changed_fields": sorted(payload.model_fields_set)},
    )
    await session.commit()
    return {"id": source.id}


@router.post("/sources/{source_id}/scans/{scan_type}", response_model=ScanStartResponse)
async def start_scan(
    source_id: UUID,
    scan_type: ScanType,
    actor: str = Depends(require_api_actor),
) -> ScanStartResponse:
    from app.crawlers.pipeline import ScanService
    from app.database.session import async_session_factory
    from app.scheduler.tasks import run_scan_task

    service = ScanService(async_session_factory, build_default_registry())
    run = await service.create_scan(source_id, scan_type, actor=actor)
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
    return ScanStartResponse(scan_id=run.id, status=run.status.value)


@router.get("/scans/{scan_id}", dependencies=[Depends(require_api_actor)])
async def get_scan(scan_id: UUID, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    run = await session.get(ScanRun, scan_id)
    if run is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return public_model(
        run,
        "id",
        "source_id",
        "scan_type",
        "status",
        "discovered_categories",
        "scanned_entrypoints",
        "scanned_pages",
        "found_jobs",
        "new_jobs",
        "updated_jobs",
        "unchanged_jobs",
        "parsing_errors",
        "network_errors",
        "checkpoint",
        "diagnostics",
        "started_at",
        "finished_at",
    )


@router.get("/jobs", dependencies=[Depends(require_api_actor)])
async def list_jobs(
    limit: int = 50, session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 200)
    jobs = list(
        (
            await session.scalars(
                select(SourceJob).order_by(desc(SourceJob.last_seen_at)).limit(limit)
            )
        ).all()
    )
    return [
        public_model(
            job,
            "id",
            "canonical_job_id",
            "source_id",
            "external_job_id",
            "title",
            "company",
            "category",
            "salary_text",
            "location",
            "canonical_url",
            "status",
            "published_at",
            "source_updated_at",
            "first_seen_at",
            "last_seen_at",
        )
        for job in jobs
    ]


@router.get("/matches", dependencies=[Depends(require_api_actor)])
async def list_matches(
    limit: int = 50, session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    values = list(
        (
            await session.scalars(
                select(MatchEvaluation)
                .order_by(desc(MatchEvaluation.created_at))
                .limit(min(limit, 200))
            )
        ).all()
    )
    return [
        public_model(
            item,
            "id",
            "canonical_job_id",
            "source_job_id",
            "resume_fit",
            "preference_fit",
            "overall_fit",
            "requirements_met",
            "missing_requirements",
            "risks",
            "scam_indicators",
            "explanation",
            "decision",
            "model",
            "created_at",
        )
        for item in values
    ]


@router.post("/applications/prepare")
async def prepare_application(
    payload: ApplicationPrepareInput,
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        application = await ApplicationService(get_settings()).prepare(
            session, payload.canonical_job_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="canonical job not found") from exc
    except ApplicationPreparationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await record_audit_event(
        session,
        actor=actor,
        action="application.prepared",
        entity_type="application",
        entity_id=str(application.id),
        correlation_id=str(application.id),
        decision=application.status.value,
    )
    await session.commit()
    return {"id": application.id, "status": application.status.value}


@router.post("/applications/{application_id}/send")
async def send_application(
    application_id: UUID, actor: str = Depends(require_api_actor)
) -> dict[str, Any]:
    from app.database.session import async_session_factory

    try:
        delivery = await EmailService(get_settings(), async_session_factory).send_application(
            application_id
        )
    except LookupError as exc:
        await _audit_send_request(
            async_session_factory,
            actor=actor,
            application_id=application_id,
            decision="rejected",
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=404, detail="application not found") from exc
    except EmailSendBlocked as exc:
        await _audit_send_request(
            async_session_factory,
            actor=actor,
            application_id=application_id,
            decision="rejected",
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=409, detail="application is not eligible for delivery"
        ) from exc
    await _audit_send_request(
        async_session_factory,
        actor=actor,
        application_id=application_id,
        decision=delivery.status.value,
    )
    return {"application_id": application_id, "delivery_status": delivery.status.value}


@router.get("/applications/{application_id}", dependencies=[Depends(require_api_actor)])
async def application_detail(
    application_id: UUID, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        return await get_application_detail(session, application_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="application not found") from exc


@router.post("/applications/{application_id}/reconcile-delivery-unknown")
async def reconcile_application_delivery(
    application_id: UUID,
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        delivery = await reconcile_stale_delivery_unknown(
            session,
            application_id,
            actor=actor,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="application not found") from exc
    except DeliveryReconciliationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return {
        "application_id": application_id,
        "delivery_id": delivery.id,
        "delivery_status": delivery.status.value,
    }


@router.get("/applications", dependencies=[Depends(require_api_actor)])
async def list_applications(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    values = list(
        (await session.scalars(select(Application).order_by(desc(Application.created_at)))).all()
    )
    return [
        public_model(
            item,
            "id",
            "canonical_job_id",
            "source_job_id",
            "resume_id",
            "recipient_contact_id",
            "subject",
            "language",
            "status",
            "policy_decision",
            "policy_result",
            "created_at",
            "sent_at",
        )
        for item in values
    ]


@router.get("/alerts", dependencies=[Depends(require_api_actor)])
async def list_alerts(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    values = list(
        (await session.scalars(select(Alert).order_by(desc(Alert.created_at)).limit(200))).all()
    )
    return [
        public_model(item, "id", "source_id", "severity", "code", "message", "created_at")
        for item in values
    ]


@router.get("/audit", dependencies=[Depends(require_api_actor)])
async def list_audit(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    values = list(
        (
            await session.scalars(
                select(AuditEvent).order_by(desc(AuditEvent.timestamp)).limit(200)
            )
        ).all()
    )
    return [
        public_model(
            item,
            "id",
            "actor",
            "action",
            "entity_type",
            "entity_id",
            "decision",
            "sanitized_details",
            "correlation_id",
            "timestamp",
        )
        for item in values
    ]


@router.get("/oauth/gmail/start")
async def gmail_oauth_start(
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    from uuid import uuid4

    from app.email.oauth import (
        GMAIL_OAUTH_BINDING_COOKIE,
        OAUTH_STATE_TTL_SECONDS,
        GmailOAuthError,
    )

    service = GmailOAuthService(get_settings())
    try:
        authorization = await service.create_authorization_request(session, actor=actor)
    except GmailOAuthError as exc:
        await session.rollback()
        correlation_id = exc.correlation_id or uuid4()
        await record_audit_event(
            session,
            actor=actor,
            action="oauth.gmail.start_failed",
            entity_type="oauth_authorization_request",
            entity_id=str(correlation_id),
            correlation_id=str(correlation_id),
            decision="failed",
            details={"provider": "gmail", "error_code": exc.code},
        )
        await session.commit()
        status_code = 503 if exc.code == "oauth_not_configured" else 400
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": "Gmail authorization could not start"},
        ) from exc

    await record_audit_event(
        session,
        actor=actor,
        action="oauth.gmail.started",
        entity_type="oauth_authorization_request",
        entity_id=str(authorization.request_id),
        correlation_id=str(authorization.request_id),
        decision="redirected",
        details={"provider": "gmail", "expires_at": authorization.expires_at.isoformat()},
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


@router.get("/oauth/gmail/callback")
async def gmail_oauth_callback(
    request: Request,
    state: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Any:
    from uuid import uuid4

    from fastapi.responses import JSONResponse

    from app.email.oauth import GMAIL_OAUTH_BINDING_COOKIE, GmailOAuthError

    settings = get_settings()
    service = GmailOAuthService(settings)
    admin_login = bool(state and await service.is_admin_login_request(session, state=state))
    binding_token = request.cookies.get(GMAIL_OAUTH_BINDING_COOKIE)
    try:
        if not state or not binding_token:
            raise GmailOAuthError("OAuth callback binding is missing", code="invalid_oauth_state")
        exchange = await service.exchange_callback(
            session,
            authorization_response=str(request.url),
            state=state,
            binding_token=binding_token,
        )
        audit_actor = (
            f"google:{exchange.identity.email}" if exchange.identity is not None else exchange.actor
        )
        await record_audit_event(
            session,
            actor=audit_actor,
            action="oauth.gmail.connected",
            entity_type="oauth_credential",
            entity_id=str(exchange.credential.id),
            correlation_id=str(exchange.request_id),
            decision="connected",
            details={
                "provider": "gmail",
                "scopes": list(exchange.credential.scopes),
                "identity_verified": exchange.identity is not None,
            },
        )
        if exchange.identity is not None:
            await record_audit_event(
                session,
                actor=audit_actor,
                action="admin.login.google",
                entity_type="admin_session",
                entity_id=exchange.identity.subject,
                correlation_id=str(exchange.request_id),
                decision="authenticated",
                details={"provider": "google"},
            )
        await session.commit()
    except GmailOAuthError as exc:
        await session.rollback()
        correlation_id = exc.correlation_id or uuid4()
        await record_audit_event(
            session,
            actor=exc.actor or "oauth-callback",
            action="oauth.gmail.connect_failed",
            entity_type="oauth_authorization_request",
            entity_id=str(exc.correlation_id or "gmail"),
            correlation_id=str(correlation_id),
            decision="failed",
            details={"provider": "gmail", "error_code": exc.code},
        )
        if admin_login:
            await record_audit_event(
                session,
                actor="google-login",
                action="admin.login.google_failed",
                entity_type="admin_session",
                entity_id="google",
                correlation_id=str(correlation_id),
                decision="failed",
                details={"provider": "google", "error_code": exc.code},
            )
        await session.commit()
        response = (
            RedirectResponse("/login?oauth_error=1", status_code=303)
            if admin_login
            else JSONResponse(
                status_code=400,
                content={"status": "failed", "error": exc.code},
            )
        )
    else:
        if exchange.identity is not None:
            response = RedirectResponse("/?google=connected#overview", status_code=303)
            response.set_cookie(
                settings.session_cookie_name,
                SessionSigner(settings.secret_key.get_secret_value()).issue(
                    settings.admin_username
                ),
                max_age=settings.session_ttl_seconds,
                secure=service.secure_cookie,
                httponly=True,
                samesite="strict",
                path="/",
            )
        else:
            response = JSONResponse(
                content={
                    "status": "authorized",
                    "identity_verified": False,
                    "identity_verification_reason": (
                        "gmail.send cannot independently identify the connected mailbox"
                    ),
                }
            )
    response.delete_cookie(
        GMAIL_OAUTH_BINDING_COOKIE,
        path="/api/v1/oauth/gmail/callback",
        secure=service.secure_cookie,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/oauth/gmail/status", dependencies=[Depends(require_api_actor)])
async def gmail_oauth_status(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await GmailOAuthService(get_settings()).get_status(session)


@router.delete("/oauth/gmail")
async def gmail_oauth_disconnect(
    actor: str = Depends(require_api_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    service = GmailOAuthService(get_settings())
    was_connected = await service.disconnect(session)
    await record_audit_event(
        session,
        actor=actor,
        action="oauth.gmail.disconnected",
        entity_type="oauth_credential",
        entity_id="gmail",
        correlation_id="gmail",
        decision="disconnected" if was_connected else "already_disconnected",
        details={
            "provider": "gmail",
            "pending_authorizations_invalidated": True,
            "remote_grant_revoked": False,
        },
    )
    await session.commit()
    return {
        "provider": "gmail",
        "connected": False,
        "was_connected": was_connected,
        "remote_grant_revoked": False,
    }
