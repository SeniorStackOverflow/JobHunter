"""The account-scoped workspace shared by invited users and Google admins."""

from __future__ import annotations

# FastAPI declarative dependency/form defaults intentionally call Depends/Form.
# ruff: noqa: B008
import contextlib
from collections.abc import Sequence
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.routes import _daily_application_rules
from app.applications import get_application_detail
from app.applications.service import ApplicationPreparationError, ApplicationService
from app.audit import record_audit_event
from app.auth.access import (
    require_account,
    require_owned_profile,
    require_user_csrf,
    require_user_feature,
    settings,
)
from app.database import get_session
from app.database.session import async_session_factory
from app.email.service import EmailSendBlocked, EmailService
from app.learning import ReviewLearningService, fixed_preference_dimensions
from app.models.entities import (
    Account,
    Application,
    Resume,
    UserProfile,
)
from app.models.enums import (
    ProfileStatus,
    ReviewOutcome,
    ReviewReason,
)
from app.profiles import ProfileService, ResumeService
from app.profiles.schemas import JobPreferenceUpdateInput, UserProfileInput
from app.profiles.service import ResumeDeletion, ResumeInUseError
from app.profiles.sources import set_source_selected
from app.security.files import UnsafeResumeError, read_verified_resume

router = APIRouter(tags=["user-workspace"])


def _items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _url(view: str, profile_id: UUID | None, *, notice: str | None = None) -> str:
    target = f"/app?view={view}"
    if profile_id is not None:
        target += f"&profile_id={profile_id}"
    if notice:
        target += f"&notice={notice}"
    return target


async def _actor_profile(
    request: Request, session: AsyncSession, profile_id: UUID
) -> tuple[Account, UserProfile]:
    account, _ = await require_account(request, session)
    profile = await require_owned_profile(session, account.id, profile_id)
    return account, profile


async def _owned_application(
    session: AsyncSession, account_id: UUID, application_id: UUID
) -> Application:
    application = await session.scalar(
        select(Application)
        .join(UserProfile, UserProfile.id == Application.profile_id)
        .where(
            Application.id == application_id,
            UserProfile.owner_account_id == account_id,
        )
    )
    if application is None:
        raise HTTPException(status_code=404, detail="application not found")
    return application


async def render_user_dashboard(
    request: Request,
    session: AsyncSession,
    account: Account,
    session_token: str,
    *,
    view: str = "overview",
    profile_id: UUID | None = None,
    page: int = 1,
    notice: str | None = None,
    invite_token: str | None = None,
    q: str = "",
    status_filter: str = "pending_review",
    history_kind: str = "all",
) -> Response:
    from app.ui.dashboard import render_dashboard

    return await render_dashboard(
        request,
        session,
        settings(),
        session_token,
        account=account,
        view=view,
        profile_id=profile_id,
        page=page,
        notice=notice,
        invite_token=invite_token,
        q=q,
        status_filter=status_filter,
        history_kind=history_kind,
    )


@router.post("/app/profiles/{profile_id}/update")
async def update_user_profile(
    profile_id: UUID,
    request: Request,
    name: str = Form(...),
    contact_email: str = Form(""),
    phone: str = Form(""),
    location: str = Form(""),
    languages: str = Form(""),
    skills: str = Form(""),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, profile = await _actor_profile(request, session, profile_id)
    try:
        payload = UserProfileInput(
            name=name.strip(),
            contact_email=contact_email.strip() or None,
            phone=phone.strip() or None,
            location=location.strip() or None,
            languages=[{"code": item, "confirmed": True} for item in _items(languages)],
            skills=_items(skills),
            work_experience=profile.work_experience,
            education=profile.education,
            driving_licences=profile.driving_licences,
            confirmed_facts=profile.confirmed_facts,
            availability=profile.availability,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid profile fields") from exc
    await ProfileService().upsert_profile(session, payload, profile.id, owner_account_id=account.id)
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="profile.updated",
        entity_type="user_profile",
        entity_id=str(profile.id),
        correlation_id=str(profile.id),
    )
    await session.commit()
    return RedirectResponse(_url("settings", profile.id, notice="profile_saved"), status_code=303)


@router.post("/app/profiles/{profile_id}/activate")
async def activate_user_profile(
    profile_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, profile = await _actor_profile(request, session, profile_id)
    if profile.status != ProfileStatus.DRAFT:
        raise HTTPException(status_code=409, detail="only a draft profile can be activated")
    ready_resume_id = await session.scalar(
        select(Resume.id)
        .where(
            Resume.profile_id == profile.id,
            Resume.active.is_(True),
            Resume.verified.is_(True),
        )
        .limit(1)
    )
    if ready_resume_id is None:
        raise HTTPException(
            status_code=409,
            detail="verified active resume required before activation",
        )
    profile.status = ProfileStatus.ACTIVE
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="profile.activated",
        entity_type="user_profile",
        entity_id=str(profile.id),
        correlation_id=str(profile.id),
        decision="active",
    )
    await session.commit()
    return RedirectResponse(
        _url("overview", profile.id, notice="profile_activated"), status_code=303
    )


@router.post("/app/profiles/{profile_id}/default")
async def set_user_default_profile(
    profile_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, profile = await _actor_profile(request, session, profile_id)
    await ProfileService().set_default_profile(session, profile.id, owner_account_id=account.id)
    await session.commit()
    return RedirectResponse(_url("settings", profile.id, notice="profile_default"), status_code=303)


@router.post("/app/profiles/{profile_id}/preferences")
async def update_user_preferences(
    profile_id: UUID,
    request: Request,
    allowed_categories: str = Form(""),
    auto_send_categories: str = Form(""),
    forbidden_categories: str = Form(""),
    allowed_cities: str = Form(""),
    minimum_daily_applications: int = Form(0, ge=0, le=100),
    daily_application_rules_present: bool = Form(False),
    maximum_daily_applications: int = Form(3, ge=0, le=100),
    minimum_auto_send_score: int = Form(85, ge=0, le=100),
    remote_allowed: bool = Form(False),
    consider_outside_primary_resume: bool = Form(False),
    willing_without_experience: bool = Form(False),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, profile = await _actor_profile(request, session, profile_id)
    if daily_application_rules_present and minimum_daily_applications > maximum_daily_applications:
        raise HTTPException(
            status_code=422, detail="minimum daily applications cannot exceed maximum"
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
    preferences = await ProfileService().update_preferences(session, payload, profile.id)
    if daily_application_rules_present:
        preferences.additional_rules = _daily_application_rules(
            preferences.additional_rules, minimum=minimum_daily_applications, force_minimum=False
        )
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="preferences.updated",
        entity_type="user_profile",
        entity_id=str(profile.id),
        correlation_id=str(profile.id),
    )
    await session.commit()
    return RedirectResponse(
        _url("settings", profile.id, notice="preferences_saved"), status_code=303
    )


@router.post("/app/profiles/{profile_id}/sources/{source_id}")
async def select_user_source(
    profile_id: UUID,
    source_id: UUID,
    request: Request,
    enabled: bool = Form(...),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, profile = await _actor_profile(request, session, profile_id)
    try:
        await set_source_selected(
            session,
            profile_id=profile.id,
            source_id=source_id,
            enabled=enabled,
            owner_account_id=account.id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="source not found") from exc
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="profile_source.selected" if enabled else "profile_source.deselected",
        entity_type="user_profile",
        entity_id=str(profile.id),
        correlation_id=str(profile.id),
        details={"source_id": str(source_id)},
    )
    await session.commit()
    return RedirectResponse(
        _url("settings", profile.id, notice="source_selection_saved"), status_code=303
    )


@router.post("/app/profiles/{profile_id}/auto-send/{action}")
async def change_user_auto_send(
    profile_id: UUID,
    action: str,
    request: Request,
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, profile = await _actor_profile(request, session, profile_id)
    if profile.status != ProfileStatus.ACTIVE or action not in {"pause", "resume"}:
        raise HTTPException(status_code=409, detail="profile is not active")
    service = ProfileService()
    if action == "pause":
        await service.pause_auto_send(session, profile.id)
    else:
        await service.resume_auto_send(session, profile.id)
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action=f"auto_send.{action}",
        entity_type="user_profile",
        entity_id=str(profile.id),
        correlation_id=str(profile.id),
    )
    await session.commit()
    return RedirectResponse(
        _url(
            "overview",
            profile.id,
            notice=f"auto_send_{'paused' if action == 'pause' else 'resumed'}",
        ),
        status_code=303,
    )


@router.post("/app/profiles/{profile_id}/resumes")
async def upload_user_resume(
    profile_id: UUID,
    request: Request,
    name: str = Form(...),
    category: str = Form(...),
    file: UploadFile = File(...),
    make_default: bool = Form(False),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, profile = await _actor_profile(request, session, profile_id)
    current_settings = settings()
    data = await file.read(current_settings.max_resume_bytes + 1)
    service = ResumeService(current_settings)
    try:
        resume = await service.upload(
            session,
            profile_id=profile.id,
            name=name.strip(),
            category=category.strip(),
            filename=file.filename or "resume.pdf",
            mime_type=file.content_type or "",
            data=data,
            make_default=make_default,
        )
        await record_audit_event(
            session,
            actor=f"account:{account.id}",
            action="resume.uploaded",
            entity_type="resume",
            entity_id=str(resume.id),
            correlation_id=str(resume.id),
            details={"sha256": resume.sha256},
        )
    except UnsafeResumeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        with contextlib.suppress(Exception):
            service.abort_last_pending_upload()
        raise
    try:
        await session.commit()
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        if await service.resolve_last_upload_commit_outcome() != "committed":
            raise
    service.finalize_upload(resume)
    return RedirectResponse(_url("settings", profile.id, notice="resume_uploaded"), status_code=303)


@router.get("/app/resumes/{resume_id}/file")
async def user_resume_file(
    resume_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    require_user_feature()
    account, _ = await require_account(request, session)
    resume = await session.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="resume not found")
    await require_owned_profile(session, account.id, resume.profile_id)
    if resume.storage_key.startswith("pending/"):
        raise HTTPException(status_code=404, detail="resume file unavailable")
    current_settings = settings()
    try:
        data = read_verified_resume(
            current_settings.resume_storage_path,
            resume.storage_key,
            expected_sha256=resume.sha256,
            expected_mime_type=resume.mime_type,
            max_bytes=current_settings.max_resume_bytes,
        )
    except UnsafeResumeError as exc:
        raise HTTPException(status_code=404, detail="resume file unavailable") from exc
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(resume.original_filename)}",
            "Cache-Control": "no-store",
        },
    )


@router.post("/app/resumes/{resume_id}/{action}")
async def change_user_resume(
    resume_id: UUID,
    action: str,
    request: Request,
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, _ = await require_account(request, session)
    resume = await session.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="resume not found")
    profile = await require_owned_profile(session, account.id, resume.profile_id)
    service = ResumeService(settings())
    if action == "verify":
        if resume.storage_key.startswith("pending/"):
            raise HTTPException(status_code=409, detail="resume file unavailable")
        try:
            read_verified_resume(
                settings().resume_storage_path,
                resume.storage_key,
                expected_sha256=resume.sha256,
                expected_mime_type=resume.mime_type,
                max_bytes=settings().max_resume_bytes,
            )
        except UnsafeResumeError as exc:
            raise HTTPException(status_code=409, detail="resume file unavailable") from exc
        resume.verified = True
        resume.active = True
    elif action == "delete":
        deletion: ResumeDeletion | None = None
        try:
            deletion = await service.delete(session, resume_id)
            await record_audit_event(
                session,
                actor=f"account:{account.id}",
                action="resume.deleted",
                entity_type="resume",
                entity_id=str(resume_id),
                correlation_id=str(resume_id),
                details=deletion.audit_details(),
            )
        except ResumeInUseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            with contextlib.suppress(Exception):
                await session.rollback()
            with contextlib.suppress(Exception):
                if deletion is not None:
                    service.abort_pending_delete(deletion)
            raise
        try:
            await session.commit()
        except Exception:
            with contextlib.suppress(Exception):
                await session.rollback()
            if (
                deletion is None
                or await service.resolve_delete_commit_outcome(deletion) != "committed"
            ):
                raise
        service.finalize_delete(deletion)
        return RedirectResponse(
            _url("settings", profile.id, notice="resume_deleted"), status_code=303
        )
    elif action in {"activate", "deactivate", "archive", "restore"}:
        operation = getattr(service, action)
        try:
            await operation(session, resume_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    else:
        raise HTTPException(status_code=404, detail="unsupported resume action")
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action=f"resume.{action}",
        entity_type="resume",
        entity_id=str(resume.id),
        correlation_id=str(resume.id),
    )
    await session.commit()
    return RedirectResponse(_url("settings", profile.id, notice="resume_updated"), status_code=303)


@router.get("/app/applications/{application_id}")
async def user_application_detail(
    application_id: UUID,
    request: Request,
    notice: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    require_user_feature()
    account, session_token = await require_account(request, session)
    application = await _owned_application(session, account.id, application_id)
    profile = await require_owned_profile(session, account.id, application.profile_id)
    from app.ui.dashboard import render_dashboard

    detail = await get_application_detail(session, application.id)
    return await render_dashboard(
        request,
        session,
        settings(),
        session_token,
        account=account,
        profile_id=profile.id,
        view="decisions",
        notice=notice,
        template_name="application_detail.html",
        extra_context={"application": detail, "view_title": "Проверка отклика"},
    )


@router.post("/app/applications/{application_id}/approve")
async def approve_user_application(
    application_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, _ = await require_account(request, session)
    await _owned_application(session, account.id, application_id)
    try:
        application = await ApplicationService(settings()).approve(session, application_id)
    except ApplicationPreparationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await ReviewLearningService().record_decision(
        session,
        application,
        outcome=ReviewOutcome.APPROVED,
        actor=f"account:{account.id}",
    )
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="application.approved",
        entity_type="application",
        entity_id=str(application.id),
        correlation_id=str(application.id),
        decision=application.status.value,
    )
    await session.commit()
    return RedirectResponse(
        f"/app/applications/{application.id}?notice=application_approved", status_code=303
    )


@router.post("/app/applications/{application_id}/reject")
async def reject_user_application(
    application_id: UUID,
    request: Request,
    reason: str = Form(""),
    reason_code: str = Form("other"),
    learn_from_review: bool = Form(True),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, _ = await require_account(request, session)
    await _owned_application(session, account.id, application_id)
    try:
        review_reason = ReviewReason(reason_code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid review reason") from exc
    try:
        application = await ApplicationService(settings()).reject(session, application_id)
    except ApplicationPreparationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await ReviewLearningService().record_decision(
        session,
        application,
        outcome=ReviewOutcome.REJECTED,
        actor=f"account:{account.id}",
        reason=review_reason,
        reason_text=reason.strip()[:500],
        learn=learn_from_review,
    )
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="application.rejected",
        entity_type="application",
        entity_id=str(application.id),
        correlation_id=str(application.id),
        decision=application.status.value,
    )
    await session.commit()
    return RedirectResponse(
        f"/app/applications/{application.id}?notice=application_rejected", status_code=303
    )


@router.post("/app/review-learning/influence")
async def set_user_learning_influence(
    request: Request,
    profile_id: UUID = Form(...),
    enabled: bool = Form(...),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, profile = await _actor_profile(request, session, profile_id)
    preferences = await ProfileService().get_preferences(session, profile.id)
    await ReviewLearningService().set_influence(
        session,
        profile.id,
        enabled=enabled,
        ignored_dimensions=fixed_preference_dimensions(preferences.allowed_cities),
    )
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="review_learning.influence_changed",
        entity_type="profile",
        entity_id=str(profile.id),
        correlation_id=str(profile.id),
        decision="enabled" if enabled else "paused",
    )
    await session.commit()
    return RedirectResponse(
        _url(
            "decisions",
            profile.id,
            notice="review_learning_enabled" if enabled else "review_learning_paused",
        ),
        status_code=303,
    )


@router.post("/app/applications/{application_id}/send")
async def send_user_application(
    application_id: UUID,
    request: Request,
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    require_user_feature()
    require_user_csrf(request, csrf_token)
    account, _ = await require_account(request, session)
    await _owned_application(session, account.id, application_id)
    try:
        delivery = await EmailService(settings(), async_session_factory).send_application(
            application_id
        )
    except EmailSendBlocked as exc:
        raise HTTPException(
            status_code=409, detail="application is not eligible for delivery"
        ) from exc
    await record_audit_event(
        session,
        actor=f"account:{account.id}",
        action="application.send_requested",
        entity_type="application",
        entity_id=str(application_id),
        correlation_id=str(application_id),
        decision=delivery.status.value,
    )
    await session.commit()
    return RedirectResponse(
        f"/app/applications/{application_id}?notice=application_sent", status_code=303
    )


__all__: Sequence[str] = ["render_user_dashboard", "router"]
