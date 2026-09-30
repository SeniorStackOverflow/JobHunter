from __future__ import annotations

# Russian UI copy is intentional.
# ruff: noqa: RUF001
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.constants import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.models.entities import JobPreference, JobSource, Resume, UserProfile
from app.models.enums import ProfileStatus, SourceHealth
from app.notifications import unread_alerts
from app.ui.panel import Panel
from app.ui.presentation import _alert_code_label


async def profile_attention(
    session: AsyncSession,
    panel: Panel,
    profile: UserProfile,
    preferences: JobPreference,
    counts: dict[str, int],
    sources: list[JobSource],
    disabled_source_ids: set[UUID],
    gmail_oauth: dict[str, Any],
) -> list[dict[str, str]]:
    is_admin = panel.is_admin
    attention_items: list[dict[str, str]] = []
    if not gmail_oauth["configured"]:
        attention_items.append(
            {
                "tone": "danger",
                "title": "Google OAuth не настроен",
                "detail": "Вход через Google и автономная отправка Gmail недоступны.",
                "href": panel.view("settings"),
                "action": "Открыть настройки",
            }
        )
    elif not gmail_oauth["connected"]:
        attention_items.append(
            {
                "tone": "danger",
                "title": "Google-аккаунт не подключён",
                "detail": "Для этого профиля нужен Gmail его владельца.",
                "href": (
                    panel.gmail_connect
                    if not is_admin or profile.owner_account_id == BOOTSTRAP_ADMIN_ACCOUNT_ID
                    else "/admin/accounts"
                ),
                "action": (
                    "Подключить"
                    if not is_admin or profile.owner_account_id == BOOTSTRAP_ADMIN_ACCOUNT_ID
                    else "Открыть пользователей"
                ),
            }
        )
    elif gmail_oauth["reauth_required"]:
        attention_items.append(
            {
                "tone": "danger",
                "title": "Gmail требует переподключения",
                "detail": "Автоотправка остановлена до получения нового OAuth-доступа.",
                "href": (
                    panel.gmail_connect
                    if not is_admin or profile.owner_account_id == BOOTSTRAP_ADMIN_ACCOUNT_ID
                    else "/admin/accounts"
                ),
                "action": (
                    "Переподключить Gmail"
                    if not is_admin or profile.owner_account_id == BOOTSTRAP_ADMIN_ACCOUNT_ID
                    else "Открыть пользователей"
                ),
            }
        )
    elif not gmail_oauth["identity_verified"]:
        attention_items.append(
            {
                "tone": "warning",
                "title": "Подтвердите Google-аккаунт",
                "detail": "Доступ к Gmail есть, но личность владельца ещё не подтверждена.",
                "href": panel.gmail_connect,
                "action": "Войти через Google",
            }
        )
    unhealthy_sources = counts["unhealthy_sources"]
    if unhealthy_sources:
        unhealthy_names = ", ".join(
            item.name
            for item in sources
            if item.enabled
            and item.id not in disabled_source_ids
            and item.health_status != SourceHealth.HEALTHY
        )
        attention_items.append(
            {
                "tone": "danger",
                "title": f"{unhealthy_sources} источников требуют проверки",
                "detail": unhealthy_names,
                "href": panel.view("settings") + "#sources",
                "action": "Проверить источники",
            }
        )
    if counts["pending_review"]:
        attention_items.append(
            {
                "tone": "warning",
                "title": f"{counts['pending_review']} откликов ждут решения",
                "detail": "Нейросеть подготовила их, но финальное действие остаётся за вами.",
                "href": panel.view("decisions"),
                "action": "Открыть очередь",
            }
        )
    if preferences.global_pause or not preferences.auto_send_enabled:
        attention_items.append(
            {
                "tone": "warning",
                "title": "Автоотправка на паузе"
                if preferences.global_pause
                else "Автоотправка выключена",
                "detail": "Автоматизация продолжит анализ, но не отправит новые отклики.",
                "href": panel.view("settings") + "#auto-send",
                "action": "Управление автоотправкой",
            }
        )
    ready_resume = await session.scalar(
        select(Resume.id)
        .where(
            Resume.profile_id == profile.id,
            Resume.active.is_(True),
            Resume.verified.is_(True),
            Resume.archived.is_(False),
        )
        .limit(1)
    )
    if ready_resume is None:
        attention_items.append(
            {
                "tone": "warning",
                "title": "Проверьте резюме",
                "detail": "Подтверждённое PDF нужно для откликов.",
                "href": panel.view("settings"),
                "action": "Открыть",
            }
        )
    if not counts["enabled_sources"]:
        attention_items.append(
            {
                "tone": "warning",
                "title": "Выберите источники",
                "detail": "Сейчас нет включённых источников для этого профиля.",
                "href": panel.view("settings"),
                "action": "Открыть",
            }
        )
    if profile.status != ProfileStatus.ACTIVE:
        attention_items.append(
            {
                "tone": "warning",
                "title": "Запустите поиск"
                if profile.status == ProfileStatus.DRAFT
                else "Поиск приостановлен",
                "detail": "Подготовьте профиль и резюме перед запуском поиска."
                if profile.status == ProfileStatus.DRAFT
                else "Для возобновления обратитесь к администратору.",
                "href": panel.view("settings"),
                "action": "Открыть",
            }
        )
    if panel.is_admin and counts.get("phone_review", 0):
        attention_items.append(
            {
                "tone": "warning",
                "title": f"{counts['phone_review']} звонков ждут проверки",
                "detail": "Проверьте итог разговора и требуемое действие.",
                "href": panel.view("calls", tab="history", filter="needs_review"),
                "action": "Открыть звонки",
            }
        )
    return attention_items


async def panel_notifications(
    session: AsyncSession, panel: Panel, attention: list[dict[str, str]]
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [dict(item) for item in attention]
    if panel.is_admin:
        for alert in await unread_alerts(session):
            items.append(
                {
                    "tone": "danger"
                    if alert.severity in {"high", "error", "critical"}
                    else "warning",
                    "title": _alert_code_label(alert.code),
                    "detail": alert.message,
                    "href": panel.view("settings") + f"#source-{alert.source_id}"
                    if alert.source_id
                    else panel.view("history", history_kind="alerts", q=str(alert.id))
                    + f"#alert-{alert.id}",
                    "action": "Проверить источник" if alert.source_id else "Открыть событие",
                    "alert_id": str(alert.id),
                }
            )
    return items


def attention_status(items: list[dict[str, Any]]) -> tuple[str, str]:
    if any(item["tone"] == "danger" for item in items):
        return "danger", "Требуется ваше внимание"
    if items:
        return "warning", "Есть задачи для вас"
    return "success", "Всё работает штатно"
