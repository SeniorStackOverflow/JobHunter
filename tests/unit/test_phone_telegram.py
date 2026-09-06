import httpx
import pytest

from app.phone.summary import CallSummary
from app.phone.telegram import (
    TelegramDeliveryError,
    render_call_notification,
    send_telegram_message,
)


@pytest.mark.asyncio
async def test_send_ok():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["json"] = request.read()
        return httpx.Response(200, json={"ok": True})

    await send_telegram_message(
        token="T",
        chat_id="42",
        text="hi",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert seen["url"] == "https://api.telegram.org/botT/sendMessage"


@pytest.mark.asyncio
async def test_send_non_2xx_raises():
    with pytest.raises(TelegramDeliveryError):
        await send_telegram_message(
            token="T",
            chat_id="42",
            text="hi",
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))),
        )


@pytest.mark.asyncio
async def test_send_network_error_raises():
    def boom(r):
        raise httpx.ConnectError("down")

    with pytest.raises(TelegramDeliveryError):
        await send_telegram_message(
            token="T",
            chat_id="42",
            text="hi",
            client=httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        )


def test_render_confident_and_uncertain_hide_caller_number():
    confident = CallSummary(
        summary_text="Собеседование в четверг.",
        outcome_guess="interview_proposed",
        proposed_datetime_text="четверг 14:00",
        proposed_address_text="ул. Индустриальная 12",
    )
    text = render_call_notification(
        confident,
        company="Example SRL",
        vacancy="Грузчик",
        session_id="abc",
        base_url="https://jobs.example.com",
    )
    assert "Example SRL" in text and "четверг 14:00" in text
    assert "https://jobs.example.com/?view=calls&session=abc" in text

    uncertain = CallSummary(summary_text="Неясно.", needs_review=True)
    u = render_call_notification(
        uncertain, company=None, vacancy=None, session_id="abc", base_url=None
    )
    assert "Требуется проверка" in u and "🔗" not in u


def test_render_escapes_html():
    s = CallSummary(summary_text="<b>x</b> & y")
    assert "&lt;b&gt;" in render_call_notification(
        s, company="A<b>", vacancy=None, session_id="s", base_url=None
    )
