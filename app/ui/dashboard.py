from __future__ import annotations

# Russian UI copy is intentional.
# ruff: noqa: RUF001
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.email.oauth import GmailOAuthService
from app.learning import (
    LearnedReviewScore,
    ReviewJobInput,
    ReviewLearningService,
    ReviewLearningSummary,
    fixed_preference_dimensions,
)
from app.matching.freshness import count_profile_matching_backlog, evaluation_is_current
from app.models.constants import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.models.entities import (
    Account,
    AccountIdentity,
    Alert,
    Application,
    AuditEvent,
    CommunicationSession,
    JobSource,
    MatchEvaluation,
    ProfileSourcePreference,
    Resume,
    ScanRun,
    SourceJob,
)
from app.models.enums import (
    ApplicationStatus,
    CommunicationChannel,
    JobStatus,
    MatchDecision,
    PhoneVerificationStatus,
    RunStatus,
    SourceHealth,
)
from app.notifications import resolved_alert_ids
from app.profiles import ProfileService
from app.security.auth import CsrfProtector
from app.settings import Settings
from app.ui.panel import Panel
from app.ui.presentation import (
    _AUDIT_ACTION_LABELS,
    _FEEDBACK_NOTICE_TONES,
    _FEEDBACK_NOTICES,
    _LOCAL_TZ,
    _VIEW_TITLES,
    _pagination,
    templates,
)
from app.ui.status import attention_status, panel_notifications, profile_attention


async def _empty_dashboard(
    request: Request,
    session: AsyncSession,
    account: Account | None,
    token: str,
    settings: Settings,
    view: str,
    notice: str | None,
    invite_token: str | None,
) -> HTMLResponse:
    assert account is not None
    identity = await session.scalar(
        select(AccountIdentity).where(AccountIdentity.account_id == account.id).limit(1)
    )
    panel = Panel(is_admin=False)
    gmail_oauth = await GmailOAuthService(settings).get_status(session, account_id=account.id)
    notifications = [
        {
            "tone": "warning",
            "title": "Создайте профиль",
            "detail": "Добавьте данные о себе, чтобы начать поиск.",
            "href": panel.view("settings"),
            "action": "Создать профиль",
        }
    ]
    response = templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "panel": panel,
            "account": account,
            "identity_email": identity.email if identity else None,
            "can_connect_gmail": True,
            "invite_token": invite_token,
            "profile": None,
            "profiles": [],
            "selected_profile_id": None,
            "view": view,
            "view_title": _VIEW_TITLES[view],
            "counts": {"pending_review": 0},
            "notifications": notifications,
            "gmail_oauth": gmail_oauth,
            "csrf_token": CsrfProtector(settings.secret_key.get_secret_value()).issue(token),
            "delivery_enabled": settings.real_email_delivery_enabled,
            "feedback_notice": _FEEDBACK_NOTICES.get(notice or ""),
            "feedback_notice_tone": "success",
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def render_dashboard(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    token: str,
    *,
    account: Account | None = None,
    profile_id: UUID | None = None,
    view: str = "overview",
    page: int = 1,
    q: str = "",
    status_filter: str = "pending_review",
    history_kind: str = "sent",
    notice: str | None = None,
    google: str | None = None,
    invite_token: str | None = None,
    template_name: str = "dashboard.html",
    extra_context: dict[str, Any] | None = None,
) -> HTMLResponse:
    is_admin = account is None
    allowed_views = (
        set(_VIEW_TITLES) if is_admin else {"overview", "decisions", "history", "settings"}
    )
    if not is_admin and view in {"calls", "diagnostics", "accounts"}:
        raise HTTPException(status_code=404, detail="page not found")
    view = view if view in allowed_views else "overview"
    q = q.strip()[:120]
    profile_service = ProfileService()
    owner_id = account.id if account is not None else None
    profiles = await profile_service.list_profiles(session, owner_account_id=owner_id)
    profile = await profile_service.get_profile(session, profile_id, owner_account_id=owner_id)
    if profile is None:
        if is_admin or profile_id is not None:
            raise HTTPException(status_code=404, detail="profile not found")
        return await _empty_dashboard(
            request, session, account, token, settings, view, notice, invite_token
        )
    panel = Panel(is_admin=is_admin, profile_id=profile.id)
    selected_profile_id = profile.id
    preferences = await profile_service.get_preferences(session, selected_profile_id)
    sources = list((await session.scalars(select(JobSource).order_by(JobSource.name))).all())
    disabled_source_ids = {
        row.source_id
        for row in (
            await session.scalars(
                select(ProfileSourcePreference).where(
                    ProfileSourcePreference.profile_id == selected_profile_id,
                    ProfileSourcePreference.enabled.is_(False),
                )
            )
        ).all()
    }
    selected_source_ids = {
        item.id for item in sources if item.enabled and item.id not in disabled_source_ids
    }

    job_visibility = (
        []
        if is_admin
        else [
            SourceJob.id.in_(
                select(MatchEvaluation.source_job_id).where(
                    MatchEvaluation.profile_id == profile.id
                )
            )
        ]
    )
    now_local = datetime.now(_LOCAL_TZ)
    start_local = datetime.combine(now_local.date(), time.min, _LOCAL_TZ)
    start = start_local.astimezone(UTC)
    end = (start_local + timedelta(days=1)).astimezone(UTC)
    today_scans = list(
        (
            await session.scalars(
                select(ScanRun).where(
                    ScanRun.source_id.in_(selected_source_ids),
                    ScanRun.started_at >= start,
                    ScanRun.started_at < end,
                )
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
    counts = {
        "jobs": int(
            await session.scalar(select(func.count(SourceJob.id)).where(*job_visibility)) or 0
        ),
        "active_jobs": int(
            await session.scalar(
                select(func.count(SourceJob.id)).where(
                    SourceJob.status == JobStatus.ACTIVE, *job_visibility
                )
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
                    ScanRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
                    ScanRun.source_id.in_(selected_source_ids),
                )
            )
            or 0
        ),
        "enabled_sources": sum(
            1 for item in sources if item.enabled and item.id not in disabled_source_ids
        ),
        "healthy_sources": sum(
            1
            for item in sources
            if item.enabled
            and item.id not in disabled_source_ids
            and item.health_status == SourceHealth.HEALTHY
        ),
    }
    counts["unhealthy_sources"] = counts["enabled_sources"] - counts["healthy_sources"]
    counts.update(notification_count=0, phone_review=0)
    if is_admin:
        counts["phone_review"] = int(
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
    matching_backlog = await count_profile_matching_backlog(
        session,
        profile,
        preferences,
        settings,
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
        "today_matches": sum(decisions.values()),
        "auto_apply": decisions.get(MatchDecision.AUTO_APPLY, 0),
        "review": decisions.get(MatchDecision.PREPARE_FOR_REVIEW, 0),
        "skip": decisions.get(MatchDecision.SKIP, 0),
        "block": decisions.get(MatchDecision.BLOCK, 0),
        "sent_today": sent_today,
        "daily_limit": preferences.maximum_daily_applications,
        "matching_backlog": matching_backlog,
    }
    gmail_oauth = await GmailOAuthService(settings).get_status(
        session, account_id=profile.owner_account_id
    )
    attention_items = await profile_attention(
        session, panel, profile, preferences, counts, sources, disabled_source_ids, gmail_oauth
    )
    notifications = await panel_notifications(session, panel, attention_items)
    counts["notification_count"] = len(notifications)
    attention_tone, overall_title = attention_status(notifications)
    overall_tone = attention_tone
    source_names = {item.id: item.name for item in sources}

    applications: list[Application] = []
    recent_applications: list[Application] = []
    application_jobs: dict[UUID, SourceJob] = {}
    application_match_evaluation_issues: dict[UUID, str] = {}
    jobs: list[SourceJob] = []
    matches: list[MatchEvaluation] = []
    match_jobs: dict[UUID, SourceJob] = {}
    scans: list[ScanRun] = []
    resumes: list[Resume] = []
    resume_usage: dict[UUID, int] = {}
    audits: list[AuditEvent] = []
    alert_states: dict[UUID, str] = {}
    historical_alerts: list[Alert] = []
    learning_summary = None
    learning_scores: dict[UUID, LearnedReviewScore] = {}
    calls_context: dict[str, Any] = {}
    pagination = _pagination(0, 1, 10)

    if template_name == "application_detail.html":
        pass
    elif view == "overview":
        recent_applications = list(
            (
                await session.scalars(
                    select(Application)
                    .where(Application.profile_id == selected_profile_id)
                    .order_by(desc(Application.created_at))
                    .limit(5)
                )
            ).all()
        )
        audits = list(
            (
                await session.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.action.in_(tuple(_AUDIT_ACTION_LABELS)),
                        *([] if is_admin else [AuditEvent.actor == f"account:{owner_id}"]),
                    )
                    .order_by(desc(AuditEvent.timestamp))
                    .limit(6)
                )
            ).all()
        )
    elif view == "decisions":
        per_page = 10 if is_admin else 25
        learning_service = ReviewLearningService()
        ignored_learning_dimensions = fixed_preference_dimensions(preferences.allowed_cities)
        learning_summary = await learning_service.summary(
            session,
            selected_profile_id,
            ignored_dimensions=ignored_learning_dimensions,
        )
        decision_statuses = {
            "pending_review": [ApplicationStatus.PENDING_REVIEW, ApplicationStatus.PREPARED],
            "approved": [ApplicationStatus.APPROVED, ApplicationStatus.AUTO_APPROVED],
            "problems": [
                ApplicationStatus.DELIVERY_UNKNOWN,
                ApplicationStatus.FAILED,
                ApplicationStatus.BLOCKED,
            ],
            "cancelled": [ApplicationStatus.CANCELLED],
            "all": list(ApplicationStatus),
        }
        status_filter = status_filter if status_filter in decision_statuses else "pending_review"
        conditions = [
            Application.profile_id == selected_profile_id,
            Application.status.in_(decision_statuses[status_filter]),
        ]
        if q:
            pattern = f"%{q}%"
            conditions.append(
                or_(
                    SourceJob.title.ilike(pattern),
                    SourceJob.company.ilike(pattern),
                    Application.subject.ilike(pattern),
                )
            )
        total = int(
            await session.scalar(
                select(func.count(Application.id))
                .select_from(Application)
                .join(SourceJob, SourceJob.id == Application.source_job_id)
                .where(*conditions)
            )
            or 0
        )
        pagination = _pagination(total, page, per_page)
        if (
            status_filter == "pending_review"
            and learning_summary.influence_enabled
            and learning_summary.approved + learning_summary.rejected > 0
        ):
            feature_rows = (
                await session.execute(
                    select(
                        Application.id,
                        Application.created_at,
                        Resume.category.label("resume_category"),
                        SourceJob.title,
                        SourceJob.company,
                        SourceJob.category,
                        SourceJob.categories_seen,
                        SourceJob.cities,
                        SourceJob.location,
                        SourceJob.schedule,
                        SourceJob.workplace_type,
                        SourceJob.employment_type,
                        SourceJob.required_experience,
                        SourceJob.no_experience,
                        SourceJob.salary_min,
                        SourceJob.salary_max,
                        SourceJob.salary_text,
                    )
                    .join(SourceJob, SourceJob.id == Application.source_job_id)
                    .join(Resume, Resume.id == Application.resume_id)
                    .where(*conditions)
                    .order_by(desc(Application.created_at))
                )
            ).all()
            ranked_ids: list[tuple[UUID, int, int]] = []
            summaries_by_resume_category: dict[str, ReviewLearningSummary] = {}
            for original_order, row in enumerate(feature_rows):
                category_summary = summaries_by_resume_category.get(row.resume_category)
                if category_summary is None:
                    category_summary = await learning_service.summary(
                        session,
                        selected_profile_id,
                        ignored_dimensions=ignored_learning_dimensions,
                        resume_category=row.resume_category,
                    )
                    summaries_by_resume_category[row.resume_category] = category_summary
                score = learning_service.score(
                    category_summary,
                    ReviewJobInput(
                        title=row.title,
                        company=row.company,
                        category=row.category,
                        categories_seen=tuple(row.categories_seen or ()),
                        cities=tuple(row.cities or ()),
                        location=row.location,
                        schedule=row.schedule,
                        workplace_type=row.workplace_type,
                        employment_type=row.employment_type,
                        required_experience=row.required_experience,
                        no_experience=row.no_experience,
                        salary_text=row.salary_text,
                        salary_min=row.salary_min,
                        salary_max=row.salary_max,
                    ),
                )
                if score is not None:
                    learning_scores[row.id] = score
                ranked_ids.append(
                    (row.id, score.value if score is not None else 50, original_order)
                )
            ranked_ids.sort(key=lambda item: (-item[1], item[2]))
            offset = (int(pagination["page"]) - 1) * per_page
            page_ids = [item[0] for item in ranked_ids[offset : offset + per_page]]
            if page_ids:
                page_items = {
                    item.id: item
                    for item in (
                        await session.scalars(
                            select(Application).where(Application.id.in_(page_ids))
                        )
                    ).all()
                }
                applications = [page_items[item_id] for item_id in page_ids]
        else:
            applications = list(
                (
                    await session.scalars(
                        select(Application)
                        .join(SourceJob, SourceJob.id == Application.source_job_id)
                        .where(*conditions)
                        .order_by(desc(Application.created_at))
                        .offset((int(pagination["page"]) - 1) * per_page)
                        .limit(per_page)
                    )
                ).all()
            )
    elif view == "history":
        valid_history_kinds = {"all", "sent", "rejected", "jobs", "matches", "scans"}
        if is_admin:
            valid_history_kinds.update({"alerts", "audit"})
        history_kind = history_kind if history_kind in valid_history_kinds else "sent"
        history_per_page = 20 if is_admin else 25
        pattern = f"%{q}%"
        if history_kind == "alerts":
            conditions = [or_(Alert.message.ilike(pattern), Alert.code.ilike(pattern))] if q else []
            if q:
                try:
                    alert_id = UUID(q)
                except ValueError:
                    pass
                else:
                    conditions = [Alert.id == alert_id]
            total = int(await session.scalar(select(func.count(Alert.id)).where(*conditions)) or 0)
            pagination = _pagination(total, page, history_per_page)
            historical_alerts = list(
                (
                    await session.scalars(
                        select(Alert)
                        .where(*conditions)
                        .order_by(desc(Alert.created_at), desc(Alert.id))
                        .offset((int(pagination["page"]) - 1) * history_per_page)
                        .limit(history_per_page)
                    )
                ).all()
            )
            resolved = await resolved_alert_ids(session, historical_alerts)
            alert_states = {
                alert.id: "resolved"
                if alert.id in resolved
                else "read"
                if alert.acknowledged
                else "unread"
                for alert in historical_alerts
            }
        elif history_kind == "audit":
            conditions = (
                [or_(AuditEvent.action.ilike(pattern), AuditEvent.actor.ilike(pattern))]
                if q
                else []
            )
            total = int(
                await session.scalar(select(func.count(AuditEvent.id)).where(*conditions)) or 0
            )
            pagination = _pagination(total, page, history_per_page)
            audits = list(
                (
                    await session.scalars(
                        select(AuditEvent)
                        .where(*conditions)
                        .order_by(desc(AuditEvent.timestamp), desc(AuditEvent.id))
                        .offset((int(pagination["page"]) - 1) * history_per_page)
                        .limit(history_per_page)
                    )
                ).all()
            )
        elif history_kind in {"all", "sent", "rejected"}:
            history_statuses = (
                [ApplicationStatus.SENT]
                if history_kind == "sent"
                else [ApplicationStatus.CANCELLED, ApplicationStatus.BLOCKED]
            )
            conditions = [Application.profile_id == selected_profile_id]
            if history_kind != "all":
                conditions.append(Application.status.in_(history_statuses))
            if q:
                conditions.append(
                    or_(
                        SourceJob.title.ilike(pattern),
                        SourceJob.company.ilike(pattern),
                        Application.subject.ilike(pattern),
                    )
                )
            total = int(
                await session.scalar(
                    select(func.count(Application.id))
                    .select_from(Application)
                    .join(SourceJob, SourceJob.id == Application.source_job_id)
                    .where(*conditions)
                )
                or 0
            )
            pagination = _pagination(total, page, history_per_page)
            applications = list(
                (
                    await session.scalars(
                        select(Application)
                        .join(SourceJob, SourceJob.id == Application.source_job_id)
                        .where(*conditions)
                        .order_by(desc(Application.sent_at), desc(Application.created_at))
                        .offset((int(pagination["page"]) - 1) * history_per_page)
                        .limit(history_per_page)
                    )
                ).all()
            )
        elif history_kind == "jobs":
            conditions = list(job_visibility)
            if q:
                conditions.append(
                    or_(
                        SourceJob.title.ilike(pattern),
                        SourceJob.company.ilike(pattern),
                        SourceJob.location.ilike(pattern),
                    )
                )
            total = int(
                await session.scalar(select(func.count(SourceJob.id)).where(*conditions)) or 0
            )
            pagination = _pagination(total, page, history_per_page)
            jobs = list(
                (
                    await session.scalars(
                        select(SourceJob)
                        .where(*conditions)
                        .order_by(desc(SourceJob.last_seen_at))
                        .offset((int(pagination["page"]) - 1) * history_per_page)
                        .limit(history_per_page)
                    )
                ).all()
            )
        elif history_kind == "matches":
            conditions = [MatchEvaluation.profile_id == selected_profile_id]
            if q:
                conditions.append(
                    or_(SourceJob.title.ilike(pattern), SourceJob.company.ilike(pattern))
                )
            total = int(
                await session.scalar(
                    select(func.count(MatchEvaluation.id))
                    .select_from(MatchEvaluation)
                    .join(SourceJob, SourceJob.id == MatchEvaluation.source_job_id)
                    .where(*conditions)
                )
                or 0
            )
            pagination = _pagination(total, page, history_per_page)
            matches = list(
                (
                    await session.scalars(
                        select(MatchEvaluation)
                        .join(SourceJob, SourceJob.id == MatchEvaluation.source_job_id)
                        .where(*conditions)
                        .order_by(desc(MatchEvaluation.created_at))
                        .offset((int(pagination["page"]) - 1) * history_per_page)
                        .limit(history_per_page)
                    )
                ).all()
            )
        else:
            conditions = [] if is_admin else [ScanRun.source_id.in_(selected_source_ids)]
            if q:
                conditions.append(JobSource.name.ilike(pattern))
            total = int(
                await session.scalar(
                    select(func.count(ScanRun.id))
                    .select_from(ScanRun)
                    .join(JobSource, JobSource.id == ScanRun.source_id)
                    .where(*conditions)
                )
                or 0
            )
            pagination = _pagination(total, page, history_per_page)
            scans = list(
                (
                    await session.scalars(
                        select(ScanRun)
                        .join(JobSource, JobSource.id == ScanRun.source_id)
                        .where(*conditions)
                        .order_by(desc(ScanRun.started_at))
                        .offset((int(pagination["page"]) - 1) * history_per_page)
                        .limit(history_per_page)
                    )
                ).all()
            )
    elif view == "settings":
        resumes = list(
            (
                await session.scalars(
                    select(Resume)
                    .where(Resume.profile_id == selected_profile_id)
                    .order_by(desc(Resume.created_at))
                )
            ).all()
        )
        resume_ids = [item.id for item in resumes]
        resume_usage = dict.fromkeys(resume_ids, 0)
        # One grouped-count query per referencing table (no N+1); sum per resume.
        if resume_ids:
            for column in (Application.resume_id, MatchEvaluation.resume_id):
                usage_rows = await session.execute(
                    select(column, func.count()).where(column.in_(resume_ids)).group_by(column)
                )
                for reference_id, reference_count in usage_rows.all():
                    if reference_id is not None:
                        resume_usage[reference_id] += int(reference_count)
    elif view == "calls":
        from app.admin.phone_routes import build_calls_context

        calls_context = await build_calls_context(
            session,
            tab=(request.query_params.get("tab") or "live"),
            page=page,
            filter_=(request.query_params.get("filter") or "all"),
            query=q,
            session_id=request.query_params.get("session"),
        )
    displayed_applications = applications or recent_applications
    application_job_ids = {item.source_job_id for item in displayed_applications}
    if application_job_ids:
        application_jobs = {
            item.id: item
            for item in (
                await session.scalars(
                    select(SourceJob).where(SourceJob.id.in_(application_job_ids))
                )
            ).all()
        }
    if view == "decisions" and applications:
        evaluation_ids = {
            item.match_evaluation_id
            for item in applications
            if item.match_evaluation_id is not None
        }
        evaluations = {
            item.id: item
            for item in (
                await session.scalars(
                    select(MatchEvaluation).where(MatchEvaluation.id.in_(evaluation_ids))
                )
            ).all()
        }
        for item in applications:
            job = application_jobs.get(item.source_job_id)
            evaluation = (
                evaluations.get(item.match_evaluation_id)
                if item.match_evaluation_id is not None
                else None
            )
            if (
                job is None
                or evaluation is None
                or evaluation.profile_id != item.profile_id
                or evaluation.source_job_id != item.source_job_id
                or evaluation.canonical_job_id != item.canonical_job_id
            ):
                application_match_evaluation_issues[item.id] = "invalid_match_evaluation_binding"
            elif not await evaluation_is_current(session, evaluation, job):
                application_match_evaluation_issues[item.id] = "match_evaluation_stale"
    match_job_ids = {item.source_job_id for item in matches}
    if match_job_ids:
        match_jobs = {
            item.id: item
            for item in (
                await session.scalars(select(SourceJob).where(SourceJob.id.in_(match_job_ids)))
            ).all()
        }

    identity = (
        await session.scalar(
            select(AccountIdentity).where(AccountIdentity.account_id == owner_id).limit(1)
        )
        if account
        else None
    )
    context = {
        "panel": panel,
        "account": account,
        "identity_email": identity.email if identity else None,
        "invite_token": invite_token,
        "notifications": notifications,
        "delivery_enabled": settings.real_email_delivery_enabled,
        "can_connect_gmail": not is_admin or profile.owner_account_id == BOOTSTRAP_ADMIN_ACCOUNT_ID,
    }
    feedback_key = "google_connected" if google == "connected" else notice or ""
    response = templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            **context,
            "csrf_token": CsrfProtector(settings.secret_key.get_secret_value()).issue(token),
            "profile": profile,
            "profile_is_admin_owned": profile.owner_account_id == BOOTSTRAP_ADMIN_ACCOUNT_ID,
            "profiles": profiles,
            "selected_profile_id": selected_profile_id,
            "view": view,
            "view_title": _VIEW_TITLES[view],
            "q": q,
            "status_filter": status_filter,
            "history_kind": history_kind,
            "pagination": pagination,
            "preferences": preferences,
            "sources": sources,
            "disabled_source_ids": disabled_source_ids,
            "source_names": source_names,
            "resumes": resumes,
            "resume_usage": resume_usage,
            "jobs": jobs,
            "matches": matches,
            "match_jobs": match_jobs,
            "applications": applications,
            "recent_applications": recent_applications,
            "application_jobs": application_jobs,
            "application_match_evaluation_issues": application_match_evaluation_issues,
            "learning_summary": learning_summary,
            "learning_scores": learning_scores,
            "scans": scans,
            "alert_states": alert_states,
            "historical_alerts": historical_alerts,
            "audits": audits,
            "counts": counts,
            "overview": overview,
            "gmail_oauth": gmail_oauth,
            "attention_items": notifications,
            "attention_tone": attention_tone,
            "overall_tone": overall_tone,
            "overall_title": overall_title,
            "now_local": now_local,
            "settings": settings,
            "feedback_notice": _FEEDBACK_NOTICES.get(feedback_key),
            "feedback_notice_tone": _FEEDBACK_NOTICE_TONES.get(feedback_key, "success"),
            # ``calls_context`` is empty for every non-calls view, so this merge
            # (tab/filter/query/calls_health/active_call/call_rows/pagination) is
            # inert elsewhere and never disturbs the existing five views.
            **calls_context,
            **(extra_context or {}),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response
