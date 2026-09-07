"""Privacy-conscious Telegram delivery for completed phone calls.

Telegram has no idempotency key. A short claim lease and an explicit
ambiguous terminal state make that limitation visible to operators.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import UUID

import httpx
from sqlalchemy import Select, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.models.entities import CallFact, CanonicalJob, CommunicationSession
from app.models.enums import CallFactState, CommunicationChannel
from app.phone.notification_state import refresh_telegram_notification
from app.settings import Settings, get_settings
from app.settings.config import TELEGRAM_REQUEST_TIMEOUT_SECONDS

_API = "https://api.telegram.org"
_MAX_MESSAGE_LENGTH = 4096
_MAX_QUOTE_LENGTH = 160
_MAX_RETRY_AFTER = 3600
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d .()\-]{6,}\d)(?!\w)")


@dataclass(frozen=True)
class TelegramDeliveryResult:
    message_id: int


class TelegramDeliveryError(RuntimeError):
    """Telegram rejected the request or its result could not be trusted."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        permanent: bool = True,
        ambiguous_delivery: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.permanent = permanent
        self.ambiguous_delivery = ambiguous_delivery
        self.retry_after = retry_after


async def send_telegram_message(
    *,
    token: str,
    chat_id: str,
    text: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = TELEGRAM_REQUEST_TIMEOUT_SECONDS,  # noqa: ASYNC109 - passed to httpx.AsyncClient, not a cancel scope
) -> TelegramDeliveryResult:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    url = f"{_API}/bot{token}/sendMessage"
    owns = client is None
    request_client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        response = await request_client.post(url, json=payload, follow_redirects=False)
    except httpx.TimeoutException as exc:
        raise TelegramDeliveryError(
            "transport_timeout", ambiguous_delivery=True, permanent=True
        ) from exc
    except httpx.RequestError as exc:
        raise TelegramDeliveryError(
            "transport_error", ambiguous_delivery=True, permanent=True
        ) from exc
    finally:
        if owns:
            await request_client.aclose()

    if response.status_code == 429:
        retry_after: int | None = None
        try:
            data = response.json()
            parameters = data.get("parameters") if isinstance(data, dict) else None
            value = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if isinstance(value, int) and not isinstance(value, bool):
                retry_after = min(max(value, 1), _MAX_RETRY_AFTER)
        except (ValueError, TypeError):
            pass
        raise TelegramDeliveryError(
            "http_429",
            status_code=429,
            retryable=True,
            permanent=False,
            retry_after=retry_after,
        )
    if 500 <= response.status_code <= 599:
        raise TelegramDeliveryError(
            f"http_{response.status_code}",
            status_code=response.status_code,
            retryable=True,
            permanent=False,
        )
    if response.status_code >= 300:
        raise TelegramDeliveryError(
            f"http_{response.status_code}", status_code=response.status_code, permanent=True
        )
    try:
        data = response.json()
    except (ValueError, TypeError) as exc:
        raise TelegramDeliveryError("invalid_response", permanent=True) from exc
    if not isinstance(data, dict):
        raise TelegramDeliveryError("invalid_response", permanent=True)
    result = data.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if (
        data.get("ok") is not True
        or not isinstance(message_id, int)
        or isinstance(message_id, bool)
    ):
        raise TelegramDeliveryError("invalid_response", permanent=True)
    return TelegramDeliveryResult(message_id=message_id)


def _clean(value: object, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return _PHONE_RE.sub("[номер скрыт]", text)[:limit]


def _short_quote(value: object) -> str:
    text = _clean(value, limit=_MAX_QUOTE_LENGTH)
    return text if len(text) < _MAX_QUOTE_LENGTH else text[: _MAX_QUOTE_LENGTH - 1] + "…"


def _status(call: object) -> str:
    raw = getattr(call, "verification_status", None)
    if raw is not None:
        return getattr(raw, "value", str(raw))
    if getattr(call, "needs_review", False):
        return "needs_review"
    return "high_confidence"


def _controlled_alternatives(call: object, field: str) -> list[tuple[str, str]]:
    summary = getattr(call, "summary", None)
    if not isinstance(summary, dict):
        return []
    verification = summary.get("verification", {})
    out: list[tuple[str, str]] = []
    sources: list[object] = []
    decision = verification.get("decision") if isinstance(verification, dict) else None
    if isinstance(decision, dict):
        sources.append(decision)
    history = verification.get("history", []) if isinstance(verification, dict) else []
    if isinstance(history, list):
        sources.extend(history)
    for source in sources:
        if isinstance(source, dict) and "facts" in source:
            source_decision: object = source
        else:
            source_decision = source.get("decision") if isinstance(source, dict) else source
        candidates = source_decision.get("facts", []) if isinstance(source_decision, dict) else []
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("field") != field:
                continue
            if candidate.get("state") not in {"candidate", "conflict", "confirmed"}:
                continue
            value = _clean(
                candidate.get("normalized_value") or candidate.get("raw_expression"), limit=160
            )
            try:
                UUID(str(candidate.get("source_turn_id", "")))
            except (TypeError, ValueError, AttributeError):
                continue
            quote = _short_quote(candidate.get("supporting_quote"))
            if not quote:
                continue
            pair = (value, quote)
            if value and pair not in out:
                out.append(pair)
    return out[:4]


def _admin_link(base_url: str | None, session_id: object) -> str:
    if not base_url or not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return ""
    parts = urlsplit(base_url)
    try:
        _ = parts.port
    except ValueError:
        return ""
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(char in base_url for char in "<>\"'\\")
        or any(ord(char) < 32 or char.isspace() for char in parts.hostname or "")
    ):
        return ""
    path = f"{parts.path.rstrip('/')}/"
    query = urlencode({"view": "calls", "session": quote(session_id, safe="")})
    url = urlunsplit((parts.scheme, parts.netloc, path, query, ""))
    return f"\n🔗 {escape(url, quote=True)}"


def _truncate(text: str) -> str:
    if len(text.encode("utf-8")) <= _MAX_MESSAGE_LENGTH:
        return text
    text = text[:_MAX_MESSAGE_LENGTH]
    while len(text.encode("utf-8")) > _MAX_MESSAGE_LENGTH - len("…".encode()):
        text = text[:-1]
    partial = re.search(r"&(?:[A-Za-z][A-Za-z0-9]{0,15}|#[0-9]{1,7})?$", text)
    return (text[: partial.start()] if partial else text) + "…"


def render_call_notification(
    call: object | None = None,
    facts: Sequence[CallFact] = (),
    *,
    company: str | None = None,
    vacancy: str | None = None,
    base_url: str | None = None,
    session_id: str | None = None,
) -> str:
    """Render Russian status-aware content without exposing caller numbers."""
    # The first positional CallSummary/session_id form remains accepted for the
    # original Phase 2b caller; new code passes the persisted call and facts.
    status = _status(call)
    actual_session_id = session_id or str(getattr(call, "id", ""))
    title = _clean(company or "Неизвестная компания", limit=180)
    if vacancy:
        title = f"{title} — {_clean(vacancy, limit=180)}"
    lines = [f"📞 {escape(title)}"]
    labels = {
        "confirmed": "Подтверждено",
        "high_confidence": "Высокая уверенность",
        "needs_review": "Требуется проверка",
        "pending": "Ожидает проверки",
        "not_applicable": "Статус не определён",
    }
    lines.append(f"Статус: {escape(labels.get(status, 'Требуется проверка'))}")

    if facts:
        field_labels = {
            "interview_date": "Дата",
            "interview_time": "Время",
            "timezone": "Часовой пояс",
            "format": "Формат",
            "address": "Место",
            "meeting_url": "Ссылка",
        }
        alternatives: list[str] = []
        for fact in facts:
            label = field_labels.get(fact.field, _clean(fact.field, limit=64))
            value = _clean(fact.normalized_value or fact.raw_expression, limit=220)
            if not value:
                continue
            if fact.state is CallFactState.CONFLICT:
                pairs = _controlled_alternatives(call, fact.field)
                if pairs:
                    alternatives.extend(
                        (
                            f"{label}: {candidate} (фраза: «{quote}»)"
                            if quote
                            else f"{label}: {candidate}"
                        )
                        for candidate, quote in pairs
                    )
                else:
                    alternatives.append(f"{label}: значение расходится")
                continue
            if fact.state is CallFactState.UNKNOWN:
                alternatives.append(f"{label}: значение не установлено")
                continue
            marker = "Подтверждено" if fact.state is CallFactState.CONFIRMED else "Предварительно"
            supporting_quote = next(
                (
                    quote
                    for candidate, quote in _controlled_alternatives(call, fact.field)
                    if candidate == value and quote
                ),
                "",
            )
            quote_suffix = f" (фраза: «{supporting_quote}»)" if supporting_quote else ""
            lines.append(f"{marker} — {escape(label)}: {escape(value + quote_suffix)}")
        if alternatives:
            lines.append("Варианты для проверки:")
            lines.extend(f"• {escape(item)}" for item in alternatives)
    else:
        persisted_summary = getattr(call, "summary", None)

        def summary_text_value(key: str) -> str:
            if isinstance(persisted_summary, dict):
                value = persisted_summary.get(key)
                return value if isinstance(value, str) else ""
            # Retain the legacy value-object form used by the pre-persistence
            # renderer callers; CommunicationSession always takes the mapping path.
            value = getattr(call, key, "")
            return value if isinstance(value, str) else ""

        summary_text = _clean(summary_text_value("summary_text"), limit=900)
        if summary_text:
            lines.append(escape(summary_text))
        proposed_datetime = _clean(summary_text_value("proposed_datetime_text"), limit=220)
        proposed_address = _clean(summary_text_value("proposed_address_text"), limit=220)
        if proposed_datetime:
            lines.append(f"🕒 {escape(proposed_datetime)}")
        if proposed_address:
            lines.append(f"📍 {escape(proposed_address)}")
        if getattr(call, "needs_review", False):
            lines.append("⚠️ Требуется проверка — откройте запись звонка.")
    return _truncate("\n".join(lines) + _admin_link(base_url, actual_session_id))


def _telegram_record(summary: object) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    value = summary.get("telegram")
    return dict(value) if isinstance(value, dict) else {}


async def _job_company_vacancy(
    db: AsyncSession, call: CommunicationSession
) -> tuple[str | None, str | None]:
    if call.canonical_job_id is None:
        return None, None
    job = await db.get(CanonicalJob, call.canonical_job_id)
    if job is None:
        return None, None
    return job.normalized_company, job.normalized_title


async def _load_claimed(db: AsyncSession, call_id: Any, token: str) -> CommunicationSession | None:
    query = (
        select(CommunicationSession)
        .where(CommunicationSession.id == call_id)
        .execution_options(populate_existing=True)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    call = await db.scalar(query)
    if call is None or _telegram_record(call.summary).get("claim_token") != token:
        return None
    return call


async def _store_claimed_summary(
    db: AsyncSession,
    *,
    call: CommunicationSession,
    token: str,
    old_summary: dict[str, Any],
    new_summary: dict[str, Any],
) -> bool:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        call.summary = new_summary
        return True
    with db.no_autoflush:
        result = await db.execute(
            update(CommunicationSession)
            .where(
                CommunicationSession.id == call.id,
                CommunicationSession.summary["telegram"]["claim_token"].as_string() == token,
            )
            .values(summary=new_summary)
        )
    return int(getattr(result, "rowcount", 0)) == 1


def _due_query(
    *, now: datetime, lease_before: datetime, limit: int
) -> Select[tuple[CommunicationSession]]:
    return (
        select(CommunicationSession)
        .where(
            CommunicationSession.channel == CommunicationChannel.CALL,
            CommunicationSession.summary_state == "done",
            CommunicationSession.summary["telegram"]["state"]
            .as_string()
            .in_(("pending", "retrying")),
            or_(
                CommunicationSession.summary["telegram"]["next_attempt_at"].as_string().is_(None),
                CommunicationSession.summary["telegram"]["next_attempt_at"].as_string()
                <= now.isoformat(),
            ),
            or_(
                CommunicationSession.summary["telegram"]["claimed_at"].as_string().is_(None),
                CommunicationSession.summary["telegram"]["claimed_at"].as_string()
                <= lease_before.isoformat(),
            ),
        )
        .order_by(CommunicationSession.ended_at, CommunicationSession.id)
        .limit(limit)
    )


async def _claim_due(
    db: AsyncSession, *, settings: Settings, now: datetime, batch: int = 1
) -> list[tuple[Any, str]]:
    claimed: list[tuple[Any, str]] = []
    lease_before = now - timedelta(seconds=settings.phone_telegram_lease_seconds)
    async with db.begin():
        query = _due_query(
            now=now,
            lease_before=lease_before,
            limit=max(1, min(batch, settings.phone_telegram_batch)),
        )
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        rows = list((await db.scalars(query)).all())
        for call in rows:
            record = _telegram_record(call.summary)
            token = secrets.token_urlsafe(32)
            updated_record = {
                "input_revision": int(record.get("input_revision", call.verification_revision)),
                "state": "pending",
                "attempts": int(record.get("attempts", 0)),
                "next_attempt_at": record.get("next_attempt_at"),
                "message_id": record.get("message_id"),
                "ambiguous_delivery": bool(record.get("ambiguous_delivery", False)),
                "claim_token": token,
                "claimed_at": now.isoformat(),
            }
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                call.summary = {**call.summary, "telegram": updated_record}
                claimed.append((call.id, token))
            else:
                old_summary = call.summary
                result = await db.execute(
                    update(CommunicationSession)
                    .where(
                        CommunicationSession.id == call.id,
                        CommunicationSession.summary == old_summary,
                    )
                    .values(summary={**old_summary, "telegram": updated_record})
                )
                if int(getattr(result, "rowcount", 0)) == 1:
                    claimed.append((call.id, token))
            if len(claimed) >= batch:
                break
    return claimed


async def _mark_disabled(now: datetime) -> int:
    from app.database.session import async_session_factory

    count = 0
    async with async_session_factory() as db, db.begin():
        rows = list(
            (
                await db.scalars(
                    select(CommunicationSession).where(
                        CommunicationSession.channel == CommunicationChannel.CALL,
                        CommunicationSession.summary_state == "done",
                    )
                )
            ).all()
        )
        for call in rows:
            record = _telegram_record(call.summary)
            if record.get("state") not in {"pending", "retrying"}:
                continue
            call.summary = {
                **call.summary,
                "telegram": {
                    "input_revision": call.verification_revision,
                    "state": "disabled",
                    "attempts": int(record.get("attempts", 0)),
                    "next_attempt_at": None,
                    "message_id": record.get("message_id"),
                    "ambiguous_delivery": bool(record.get("ambiguous_delivery", False)),
                },
            }
            count += 1
    return count


async def _deliver_claim(call_id: Any, token: str, *, settings: Settings) -> str:
    from app.database.session import async_session_factory

    async with async_session_factory() as db:
        call = await _load_claimed(db, call_id, token)
        if call is None:
            return "skipped"
        record = _telegram_record(call.summary)
        if int(record.get("input_revision", -1)) != call.verification_revision:
            old_summary = dict(call.summary)
            refresh_telegram_notification(call)
            if not await _store_claimed_summary(
                db, call=call, token=token, old_summary=old_summary, new_summary=call.summary
            ):
                await db.rollback()
                return "skipped"
            await db.commit()
            return "skipped"
        facts = list(
            (await db.scalars(select(CallFact).where(CallFact.session_id == call.id))).all()
        )
        company, vacancy = await _job_company_vacancy(db, call)
        text = render_call_notification(
            call=call,
            facts=facts,
            company=company,
            vacancy=vacancy,
            base_url=settings.public_base_url,
        )
        revision = call.verification_revision
        attempts = int(record.get("attempts", 0)) + 1
        await db.commit()

    try:
        result = await send_telegram_message(
            token=settings.telegram_bot_token.get_secret_value()
            if settings.telegram_bot_token
            else "",
            chat_id=settings.telegram_chat_id or "",
            text=text,
        )
    except TelegramDeliveryError as exc:
        async with async_session_factory() as db:
            call = await _load_claimed(db, call_id, token)
            if call is None:
                return "skipped"
            record = _telegram_record(call.summary)
            if (
                call.verification_revision != revision
                or int(record.get("input_revision", -1)) != revision
            ):
                old_summary = dict(call.summary)
                refresh_telegram_notification(call)
                if not await _store_claimed_summary(
                    db,
                    call=call,
                    token=token,
                    old_summary=old_summary,
                    new_summary=call.summary,
                ):
                    await db.rollback()
                    return "skipped"
                await db.commit()
                return "skipped"
            if (
                exc.ambiguous_delivery
                or exc.permanent
                or attempts >= settings.phone_telegram_max_attempts
            ):
                state = "failed"
                next_attempt = None
            else:
                state = "retrying"
                delay = exc.retry_after or min(
                    settings.phone_telegram_retry_max_seconds,
                    settings.phone_telegram_retry_base_seconds * (2 ** max(attempts - 1, 0)),
                )
                next_attempt = (utcnow() + timedelta(seconds=delay)).isoformat()
            old_summary = dict(call.summary)
            new_summary = {
                **call.summary,
                "telegram": {
                    "input_revision": revision,
                    "state": state,
                    "attempts": attempts,
                    "next_attempt_at": next_attempt,
                    "message_id": None,
                    "ambiguous_delivery": bool(exc.ambiguous_delivery),
                },
            }
            if not await _store_claimed_summary(
                db, call=call, token=token, old_summary=old_summary, new_summary=new_summary
            ):
                await db.rollback()
                return "skipped"
            await db.commit()
        return state

    async with async_session_factory() as db:
        call = await _load_claimed(db, call_id, token)
        if call is None:
            return "skipped"
        record = _telegram_record(call.summary)
        if (
            call.verification_revision != revision
            or int(record.get("input_revision", -1)) != revision
        ):
            old_summary = dict(call.summary)
            refresh_telegram_notification(call)
            if not await _store_claimed_summary(
                db, call=call, token=token, old_summary=old_summary, new_summary=call.summary
            ):
                await db.rollback()
                return "skipped"
            await db.commit()
            return "skipped"
        old_summary = dict(call.summary)
        new_summary = {
            **call.summary,
            "telegram": {
                "input_revision": revision,
                "state": "sent",
                "attempts": attempts,
                "next_attempt_at": None,
                "message_id": result.message_id,
                "ambiguous_delivery": False,
            },
        }
        if not await _store_claimed_summary(
            db, call=call, token=token, old_summary=old_summary, new_summary=new_summary
        ):
            await db.rollback()
            return "skipped"
        await db.commit()
    return "sent"


async def deliver_pending_phone_notifications() -> dict[str, int]:
    from app.database.session import async_session_factory

    settings = get_settings()
    counters = {"picked": 0, "sent": 0, "retrying": 0, "failed": 0, "disabled": 0, "skipped": 0}
    if (
        not settings.telegram_enabled
        or settings.telegram_bot_token is None
        or not settings.telegram_chat_id
    ):
        counters["disabled"] = await _mark_disabled(utcnow())
        return counters
    for _ in range(settings.phone_telegram_batch):
        async with async_session_factory() as db:
            claimed = await _claim_due(db, settings=settings, now=utcnow(), batch=1)
        if not claimed:
            break
        counters["picked"] += 1
        call_id, token = claimed[0]
        result = await _deliver_claim(call_id, token, settings=settings)
        counters[result] = counters.get(result, 0) + 1
        if result == "skipped":
            break
    return counters


__all__ = [
    "TelegramDeliveryError",
    "TelegramDeliveryResult",
    "deliver_pending_phone_notifications",
    "render_call_notification",
    "send_telegram_message",
]
