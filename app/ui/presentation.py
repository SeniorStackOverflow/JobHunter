from __future__ import annotations

# Russian UI copy is intentional.
# ruff: noqa: RUF001
from datetime import UTC, datetime
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.models.entities import Application
from app.security.ssrf import public_url_shape_is_safe

templates = Jinja2Templates(directory=["app/admin/templates", "app/auth/templates"])
_ADMIN_STATIC_ROOT = Path(__file__).resolve().parents[1] / "admin" / "static"


@lru_cache
def _admin_asset_url(filename: str) -> str:
    """Return a content-versioned URL so a deploy cannot reuse stale browser JavaScript."""
    if Path(filename).name != filename:
        raise ValueError("admin asset filename must not contain a path")
    content = (_ADMIN_STATIC_ROOT / filename).read_bytes()
    version = sha256(content).hexdigest()[:16]
    return f"/admin-assets/{quote(filename)}?v={version}"


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
templates.env.globals["admin_asset_url"] = _admin_asset_url


_LOCAL_TZ = ZoneInfo("Europe/Chisinau")
_STATUS_LABELS = {
    "healthy": "Работает",
    "degraded": "Есть проблемы",
    "unavailable": "Недоступно",
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
    "auto_apply": "Подходит для автоотправки",
    "prepare_for_review": "Нужна проверка",
    "skip": "Пропустить",
    "block": "Заблокировано правилами",
    "prepared": "Подготовлен",
    "skipped": "Пропущен",
    "pending_review": "На проверке",
    "approved": "Одобрен",
    "auto_approved": "Одобрен автоматически",
    "sending": "Отправляется",
    "sent": "Отправлен",
    "delivery_unknown": "Доставка неизвестна",
    "temporary_failure": "Временная ошибка",
    "permanent_failure": "Ошибка доставки",
    "blocked": "Заблокирован",
    "incremental": "Инкрементальный",
    "full": "Полный",
    "recheck": "Перепроверка",
    "authenticated": "Вход выполнен",
    "connected": "Подключено",
    "disconnected": "Отключено",
    "enabled_and_resumed": "Включено и возобновлено",
    "redirected": "Переход к Google",
}

_VIEW_TITLES = {
    "overview": "Главная",
    "decisions": "Требуют решения",
    "history": "История",
    "settings": "Настройки",
    "calls": "Звонки",
}

_AUDIT_ACTION_LABELS = {
    "admin.login.google": "Выполнен вход через Google",
    "application.approved": "Отклик одобрен",
    "application.blocked_closed_vacancy": "Отклик остановлен: вакансия закрыта",
    "application.rejected_by_owner": "Отклик отклонён",
    "application.send_requested": "Запрошена отправка отклика",
    "auto_send.paused": "Автоотправка поставлена на паузу",
    "auto_send.resumed": "Автоотправка возобновлена",
    "email.delivery": "Обновлено состояние доставки",
    "oauth.gmail.connected": "Google-аккаунт подключён",
    "oauth.gmail.disconnected": "Google-аккаунт отключён",
    "preferences.updated": "Настройки поиска обновлены",
    "profile.created": "Профиль создан",
    "profile.updated": "Профиль обновлён",
    "resume.activated": "Резюме снова активно",
    "resume.archived": "Резюме заархивировано",
    "resume.deactivated": "Резюме деактивировано",
    "resume.deleted": "Резюме удалено",
    "resume.restored": "Резюме восстановлено",
    "resume.uploaded": "Резюме загружено",
    "resume.verified": "Резюме подтверждено",
    "source.disabled": "Источник выключен",
    "source.enabled": "Источник включён",
}

_ALERT_CODE_LABELS = {
    "adapter_degradation": "Источник работает нестабильно",
    "adapter_access_degraded": "Источник временно недоступен",
    "mass_absence_suppressed": "Защитная проверка массового исчезновения вакансий",
    "email_retry_queue_stuck": "Отправка писем задерживается",
    "email_authentication_failure": "Проверьте доступ к Gmail",
}

_FEEDBACK_NOTICES = {
    "profile_activated": ("Поиск запущен", "Автоотправка остаётся на паузе."),
    "resume_updated": ("Резюме обновлено", "Состояние резюме сохранено."),
    "gmail_connected": ("Gmail подключён", "Аккаунт готов к проверке отправки."),
    "gmail_disconnected": ("Gmail отключён", "Отправка остановлена до подключения."),
    "invite_created": ("Приглашение создано", "Скопируйте ссылку сейчас."),
    "google_connected": (
        "Google подключён",
        "Вход подтверждён, доступ к Gmail сохранён на сервере.",
    ),
    "profile_saved": ("Профиль сохранён", "Изменения данных профиля применены."),
    "profile_created": ("Профиль создан", "Новый профиль готов к настройке."),
    "profile_default": ("Основной профиль изменён", "Он будет выбран по умолчанию."),
    "preferences_saved": (
        "Настройки сохранены",
        "Критерии поиска и ограничения обновлены.",
    ),
    "auto_send_paused": (
        "Автоотправка приостановлена",
        "Новые письма не будут отправляться до возобновления.",
    ),
    "auto_send_resumed": (
        "Автоотправка возобновлена",
        "JobHunter снова применяет заданные правила и дневной лимит.",
    ),
    "resume_uploaded": (
        "Резюме загружено",
        "Проверьте его перед использованием в автоматических откликах.",
    ),
    "resume_verified": ("Резюме подтверждено", "Оно доступно для подготовки откликов."),
    "resume_deactivated": (
        "Резюме деактивировано",
        "Оно больше не используется для новых откликов; активировать можно обратно.",
    ),
    "resume_activated": ("Резюме активно", "Оно снова доступно для подготовки откликов."),
    "resume_archived": (
        "Резюме заархивировано",
        "Оно скрыто из списка; строка и история сохранены. Можно восстановить.",
    ),
    "resume_restored": ("Резюме восстановлено", "Оно снова в списке, неактивно."),
    "resume_deleted": ("Резюме удалено", "Файл и запись удалены безвозвратно."),
    "profile_and_resume_created": (
        "Профиль и резюме созданы",
        "Проверьте резюме перед использованием в автоматических откликах.",
    ),
    "google_disconnected": (
        "Google отключён",
        "Отправка через Gmail остановлена до повторного подключения.",
    ),
    "alert_acknowledged": (
        "Уведомление просмотрено",
        "Оно сохранено в истории уведомлений. Просмотр не означает устранение проблемы.",
    ),
    "source_enabled": ("Источник включён", "Новые обходы снова разрешены."),
    "source_selection_saved": (
        "Источники профиля обновлены",
        "Выбор действует только для этого профиля; общие обходы не изменились.",
    ),
    "source_disabled": (
        "Источник выключен",
        "Новые обходы остановлены, собранные вакансии сохранены.",
    ),
    "scan_started": ("Проверка запущена", "Результат появится в истории обходов."),
    "application_approved": (
        "Отклик одобрен",
        "Он прошёл ручную проверку и готов к следующему этапу.",
    ),
    "application_approval_no_email": (
        "Одобрение недоступно",
        "У вакансии нет публичного email. JobHunter не отправляет отклики "
        "через внутреннюю форму сайта.",
    ),
    "application_approval_invalid_content": (
        "Письмо не прошло проверку",
        "Отклик остался в очереди и не будет отправлен. Проверьте его технические подробности.",
    ),
    "application_approval_inactive_vacancy": (
        "Одобрение недоступно",
        "Вакансия больше не активна. Отклик остался в безопасном состоянии.",
    ),
    "application_approval_stale_evaluation": (
        "Требуется повторный анализ",
        "Вакансия или данные профиля изменились. Одобрение станет доступно после переоценки.",
    ),
    "application_approval_unavailable": (
        "Одобрение не выполнено",
        "Состояние отклика изменилось или он не готов к одобрению. "
        "Обновите карточку и проверьте его ещё раз.",
    ),
    "application_rejected": (
        "Отклик отклонён",
        "Он отменён и не попадёт в отправку.",
    ),
    "application_sent": ("Письмо отправлено", "Состояние доставки сохранено в журнале."),
    "review_learning_enabled": (
        "Обучение включено",
        "Новые решения снова влияют на подсказки и порядок очереди.",
    ),
    "review_learning_paused": (
        "Влияние обучения приостановлено",
        "Решения сохраняются, но не меняют подсказки и порядок очереди.",
    ),
    "delivery_reconciled": (
        "Состояние зафиксировано",
        "Повторная отправка не выполнялась.",
    ),
}

_FEEDBACK_NOTICE_TONES = {
    "application_approval_no_email": "warning",
    "application_approval_invalid_content": "danger",
    "application_approval_inactive_vacancy": "warning",
    "application_approval_stale_evaluation": "warning",
    "application_approval_unavailable": "danger",
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
    if raw in {
        "failed",
        "blocked",
        "block",
        "permanent_failure",
        "delivery_unknown",
        "degraded",
        "unavailable",
    }:
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


def _audit_action_label(value: str) -> str:
    return _AUDIT_ACTION_LABELS.get(value, value.replace("_", " ").replace(".", " · "))


def _alert_code_label(value: str) -> str:
    if value.startswith("email_permanent_delivery_failure:"):
        return "Не удалось доставить отклик"
    return _ALERT_CODE_LABELS.get(value, "Системное уведомление")


def _application_failed_policy_rules(application: Any) -> set[str]:
    if isinstance(application, dict):
        policy_result = application.get("policy_result")
    else:
        policy_result = getattr(application, "policy_result", None)
    if not isinstance(policy_result, dict):
        return set()
    raw_rules = policy_result.get("rules_failed", [])
    if not isinstance(raw_rules, list):
        return set()
    return {str(item) for item in raw_rules}


def _application_content_validated(application: Any) -> bool:
    if isinstance(application, dict):
        return application.get("content_validated") is True
    return getattr(application, "content_validated", False) is True


def _application_safe_stop_reason(application: Any) -> str | None:
    if isinstance(application, dict):
        policy_result = application.get("policy_result")
    else:
        policy_result = getattr(application, "policy_result", None)
    if not isinstance(policy_result, dict):
        return None
    reason = policy_result.get("safe_stop_reason")
    return reason if isinstance(reason, str) and reason else None


def _application_approval_issue(
    application: Any,
    match_evaluation_issue: str | None = None,
) -> str | None:
    """Explain why a review cannot currently become an approved email application."""

    safe_stop_reason = _application_safe_stop_reason(application)
    if match_evaluation_issue is None:
        if isinstance(application, dict):
            match_evaluation_issue = application.get("match_evaluation_issue")
        else:
            match_evaluation_issue = getattr(application, "match_evaluation_issue", None)
    if (
        safe_stop_reason == "match_evaluation_stale"
        or match_evaluation_issue == "match_evaluation_stale"
    ):
        return "Вакансия изменилась — JobHunter выполняет повторный анализ."
    if safe_stop_reason == "match_evaluation_inputs_stale":
        return "Профиль или настройки изменились — JobHunter выполняет повторный анализ."
    if match_evaluation_issue == "invalid_match_evaluation_binding":
        return "Проверка соответствия вакансии недоступна — JobHunter выполняет повторный анализ."
    if _application_content_validated(application):
        return None
    if "verified_email_contact" in _application_failed_policy_rules(application):
        return (
            "У вакансии нет публичного email — JobHunter не отправляет отклики "
            "через внутреннюю форму сайта."
        )
    return "Письмо или получатель не прошли проверку безопасности."


def _application_rejection_note(application: Any) -> str | None:
    """Explain a rejection the policy made on its own, without the owner."""

    if _application_safe_stop_reason(application) == "no_public_email":
        return (
            "Отклонено автоматически: у вакансии нет публичного email — JobHunter "
            "не отправляет отклики через внутреннюю форму сайта."
        )
    return None


def _approval_failure_notice(application: Application | None, error: Exception) -> str:
    message = str(error)
    if application is not None and not _application_content_validated(application):
        if "verified_email_contact" in _application_failed_policy_rules(application):
            return "application_approval_no_email"
        return "application_approval_invalid_content"
    if message == "vacancy is no longer active":
        return "application_approval_inactive_vacancy"
    if message == "match evaluation is stale":
        return "application_approval_stale_evaluation"
    return "application_approval_unavailable"


def _pagination(total: int, requested_page: int, per_page: int) -> dict[str, int | bool]:
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, requested_page), pages)
    return {
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total": total,
        "has_previous": page > 1,
        "has_next": page < pages,
    }


templates.env.globals["status_label"] = _status_label
templates.env.globals["status_tone"] = _status_tone
templates.env.globals["format_dt"] = _format_dt
templates.env.globals["audit_action_label"] = _audit_action_label
templates.env.globals["alert_code_label"] = _alert_code_label
templates.env.globals["application_approval_issue"] = _application_approval_issue
templates.env.globals["application_rejection_note"] = _application_rejection_note
