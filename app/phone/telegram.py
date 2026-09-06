from __future__ import annotations

from html import escape

import httpx

from app.phone.summary import CallSummary

_API = "https://api.telegram.org"


class TelegramDeliveryError(RuntimeError):
    """Telegram did not accept the message."""


async def send_telegram_message(
    *,
    token: str,
    chat_id: str,
    text: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,  # noqa: ASYNC109 - passed to httpx.AsyncClient, not a cancel scope
) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    url = f"{_API}/bot{token}/sendMessage"
    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        response = await client.post(url, json=payload)
    except httpx.RequestError as exc:
        raise TelegramDeliveryError(f"transport:{type(exc).__name__}") from exc
    finally:
        if owns:
            await client.aclose()
    if response.status_code >= 300:
        raise TelegramDeliveryError(f"http_{response.status_code}")


def render_call_notification(
    summary: CallSummary,
    *,
    company: str | None,
    vacancy: str | None,
    session_id: str,
    base_url: str | None,
) -> str:
    link = f"\n🔗 {base_url}/?view=calls&session={session_id}" if base_url else ""
    body = escape(summary.summary_text)
    if summary.needs_review or summary.outcome_guess != "interview_proposed":
        head = escape(company) if company else "Неизвестная компания"
        return f"📞 {head}\n{body}\n⚠️ Требуется проверка — открой запись звонка.{link}"
    title = escape(company or "")
    if vacancy:
        title = f"{title} — {escape(vacancy)}" if title else escape(vacancy)
    parts = [f"📞 {title}".rstrip(), body]
    if summary.proposed_datetime_text:
        parts.append(f"🕒 {escape(summary.proposed_datetime_text)}")
    if summary.proposed_address_text:
        parts.append(f"📍 {escape(summary.proposed_address_text)}")
    return "\n".join(parts) + link
