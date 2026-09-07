import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.models.entities import CallFact, CommunicationSession
from app.models.enums import (
    CallFactConfirmationSource,
    CallFactState,
    CommunicationChannel,
    CommunicationDirection,
    PhoneVerificationStatus,
)
from app.phone import telegram as telegram_module
from app.phone.summary import CallSummary
from app.phone.telegram import (
    TelegramDeliveryError,
    TelegramDeliveryResult,
    render_call_notification,
    send_telegram_message,
)
from app.settings import Settings


@pytest.mark.asyncio
async def test_send_ok():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["json"] = request.read()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    result = await send_telegram_message(
        token="T",
        chat_id="42",
        text="hi",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert seen["url"] == "https://api.telegram.org/botT/sendMessage"
    assert result == TelegramDeliveryResult(message_id=77)


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
async def test_send_disables_redirects_on_injected_client():
    seen = 0

    def handler(request: httpx.Request):
        nonlocal seen
        seen += 1
        return httpx.Response(302, headers={"location": "https://redirect.invalid"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    with pytest.raises(TelegramDeliveryError):
        await send_telegram_message(token="T", chat_id="42", text="hi", client=client)
    await client.aclose()
    assert seen == 1


@pytest.mark.asyncio
async def test_send_rejects_malformed_success_envelope():
    with pytest.raises(TelegramDeliveryError) as exc:
        await send_telegram_message(
            token="T",
            chat_id="42",
            text="hi",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
            ),
        )
    assert exc.value.permanent is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "retryable", "permanent", "retry_after"),
    [
        (httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 9}}), True, False, 9),
        (httpx.Response(503), True, False, None),
        (httpx.Response(400), False, True, None),
        (httpx.Response(401), False, True, None),
        (httpx.Response(403), False, True, None),
    ],
)
async def test_send_classifies_http_status(response, retryable, permanent, retry_after):
    with pytest.raises(TelegramDeliveryError) as exc:
        await send_telegram_message(
            token="T",
            chat_id="42",
            text="hi",
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)),
        )
    assert exc.value.retryable is retryable
    assert exc.value.permanent is permanent
    assert exc.value.retry_after == retry_after


@pytest.mark.asyncio
async def test_retry_after_is_capped():
    with pytest.raises(TelegramDeliveryError) as exc:
        await send_telegram_message(
            token="T",
            chat_id="42",
            text="hi",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        429, json={"ok": False, "parameters": {"retry_after": 99999}}
                    )
                )
            ),
        )
    assert exc.value.retry_after == 3600


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
    assert "https://jobs.example.com/?view=calls&amp;session=abc" in text

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


def _call(status: PhoneVerificationStatus, summary: dict | None = None) -> CommunicationSession:
    return CommunicationSession(
        id=uuid4(),
        profile_id=uuid4(),
        channel=CommunicationChannel.CALL,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        started_at=datetime.now(UTC),
        verification_status=status,
        verification_revision=2,
        summary=summary or {},
    )


def test_render_trust_aware_facts_and_conflict_alternatives():
    call = _call(
        PhoneVerificationStatus.NEEDS_REVIEW,
        summary={
            "verification": {
                "decision": {
                    "facts": [
                        {
                            "field": "interview_time",
                            "raw_expression": "в 10:00",
                            "normalized_value": "10:00",
                            "state": "conflict",
                        },
                    ]
                },
                "history": [
                    {
                        "decision": {
                            "facts": [
                                {
                                    "field": "interview_time",
                                    "raw_expression": "в 11:00",
                                    "normalized_value": "11:00",
                                    "state": "conflict",
                                }
                            ]
                        }
                    }
                ],
                "pass_results": {"extractor": {"facts": [{"normalized_value": "<script>"}]}},
            }
        },
    )
    fact = CallFact(
        field="interview_time",
        raw_expression="в 10:00",
        normalized_value="10:00",
        state=CallFactState.CONFLICT,
    )
    text = render_call_notification(
        call=call,
        facts=[fact],
        company="A<b>",
        vacancy="Водитель",
        base_url="https://jobs.example/?x=1",
    )
    assert "Требуется проверка" in text
    assert "Варианты" in text
    assert "10:00" in text and "11:00" in text
    assert "<script>" not in text
    assert "подтверждено" not in text.casefold()
    assert "A&lt;b&gt;" in text
    assert "🔗" not in text
    assert len(text) <= 4096


def test_render_confirmed_and_high_confidence_labels_only():
    confirmed = _call(PhoneVerificationStatus.CONFIRMED)
    confirmed_fact = CallFact(
        field="interview_date",
        raw_expression="завтра",
        normalized_value="2026-09-07",
        state=CallFactState.CONFIRMED,
        confirmation_source=CallFactConfirmationSource.SMS,
    )
    text = render_call_notification(
        call=confirmed,
        facts=[confirmed_fact],
        company=None,
        vacancy=None,
        base_url=None,
    )
    assert "Подтверждено" in text
    high = _call(PhoneVerificationStatus.HIGH_CONFIDENCE)
    candidate = CallFact(
        field="interview_date",
        raw_expression="завтра",
        normalized_value="2026-09-07",
        state=CallFactState.CANDIDATE,
    )
    high_text = render_call_notification(
        call=high, facts=[candidate], company=None, vacancy=None, base_url=None
    )
    assert "Высокая уверенность" in high_text
    assert "Подтверждено" not in high_text


def test_render_limits_utf8_message_length():
    summary = CallSummary(summary_text="Привет 😀" * 3000)
    text = render_call_notification(summary, company="Компания", session_id="abc")
    assert len(text.encode("utf-8")) <= 4096


def test_render_redacts_dotted_and_international_numbers():
    text = render_call_notification(
        CallSummary(summary_text="Итог"),
        company="+1.202.555.0123 / +373 60 111 222",
        session_id="abc",
    )
    assert "+1.202.555.0123" not in text
    assert "+373 60 111 222" not in text
    assert "[номер скрыт]" in text


@pytest.mark.asyncio
async def test_transport_error_is_ambiguous_and_terminal():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("after submission", request=request)

    with pytest.raises(TelegramDeliveryError) as exc:
        await send_telegram_message(
            token="T",
            chat_id="42",
            text="hi",
            client=httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        )
    assert exc.value.ambiguous_delivery is True
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_delivery_worker_claims_and_finalizes_without_fact_state_change(
    sqlite_session_factory, monkeypatch: pytest.MonkeyPatch
):
    from app.models.entities import UserProfile
    from app.models.enums import CommunicationOutcome, PhoneSummaryState

    settings = Settings(
        _env_file=None,
        telegram_enabled=True,
        telegram_bot_token="bot-token",
        telegram_chat_id="chat-id",
        phone_telegram_batch=2,
    )
    monkeypatch.setattr(telegram_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
    sent: list[str] = []

    async def fake_send(**kwargs):
        sent.append(kwargs["text"])
        return TelegramDeliveryResult(message_id=91)

    monkeypatch.setattr(telegram_module, "send_telegram_message", fake_send)
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            outcome=CommunicationOutcome.COMPLETED,
            summary_state=PhoneSummaryState.DONE,
            verification_status=PhoneVerificationStatus.HIGH_CONFIDENCE,
            verification_revision=3,
            summary={
                "summary_text": "Готово",
                "telegram": {
                    "input_revision": 3,
                    "state": "pending",
                    "attempts": 0,
                    "next_attempt_at": None,
                },
            },
        )
        db.add(call)
        await db.commit()

    result = await telegram_module.deliver_pending_phone_notifications()
    assert result["picked"] == 1
    assert result["sent"] == 1
    assert sent
    async with sqlite_session_factory() as db:
        stored = await db.get(CommunicationSession, call.id)
        assert stored is not None
        assert stored.summary["telegram"]["state"] == "sent"
        assert stored.summary["telegram"]["message_id"] == 91
        assert stored.verification_status is PhoneVerificationStatus.HIGH_CONFIDENCE


@pytest.mark.asyncio
async def test_disabled_worker_marks_pending_without_network(
    sqlite_session_factory, monkeypatch: pytest.MonkeyPatch
):
    from app.models.entities import UserProfile
    from app.models.enums import CommunicationOutcome, PhoneSummaryState

    settings = Settings(_env_file=None, telegram_enabled=False)
    monkeypatch.setattr(telegram_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            outcome=CommunicationOutcome.COMPLETED,
            summary_state=PhoneSummaryState.DONE,
            summary={"telegram": {"state": "pending", "input_revision": 0}},
        )
        db.add(call)
        await db.commit()
    result = await telegram_module.deliver_pending_phone_notifications()
    assert result["disabled"] == 1
    async with sqlite_session_factory() as db:
        stored = await db.get(CommunicationSession, call.id)
        assert stored is not None
        assert stored.summary["telegram"]["state"] == "disabled"


@pytest.mark.asyncio
async def test_retryable_failure_is_due_again_with_bounded_backoff(
    sqlite_session_factory, monkeypatch: pytest.MonkeyPatch
):
    from app.models.entities import UserProfile
    from app.models.enums import CommunicationOutcome, PhoneSummaryState

    settings = Settings(
        _env_file=None,
        telegram_enabled=True,
        telegram_bot_token="bot-token",
        telegram_chat_id="chat-id",
        phone_telegram_retry_base_seconds=2,
        phone_telegram_retry_max_seconds=3,
    )
    monkeypatch.setattr(telegram_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)

    async def fail_send(**kwargs):
        raise TelegramDeliveryError("http_503", retryable=True, permanent=False)

    monkeypatch.setattr(telegram_module, "send_telegram_message", fail_send)
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            outcome=CommunicationOutcome.COMPLETED,
            summary_state=PhoneSummaryState.DONE,
            summary={"telegram": {"state": "pending", "input_revision": 0}},
        )
        db.add(call)
        await db.commit()
    result = await telegram_module.deliver_pending_phone_notifications()
    assert result["retrying"] == 1
    async with sqlite_session_factory() as db:
        stored = await db.get(CommunicationSession, call.id)
        assert stored is not None
        record = stored.summary["telegram"]
        assert record["state"] == "retrying"
        assert record["attempts"] == 1
        assert record["next_attempt_at"]
        assert record["ambiguous_delivery"] is False


@pytest.mark.asyncio
async def test_concurrent_workers_claim_one_record(
    sqlite_session_factory, monkeypatch: pytest.MonkeyPatch
):
    from app.models.entities import UserProfile
    from app.models.enums import CommunicationOutcome, PhoneSummaryState

    settings = Settings(
        _env_file=None,
        telegram_enabled=True,
        telegram_bot_token="bot-token",
        telegram_chat_id="chat-id",
    )
    monkeypatch.setattr(telegram_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
    calls = 0

    async def fake_send(**kwargs):
        nonlocal calls
        calls += 1
        return TelegramDeliveryResult(message_id=99)

    monkeypatch.setattr(telegram_module, "send_telegram_message", fake_send)
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            outcome=CommunicationOutcome.COMPLETED,
            summary_state=PhoneSummaryState.DONE,
            summary={"telegram": {"state": "pending", "input_revision": 0}},
        )
        db.add(call)
        await db.commit()
    results = await asyncio.gather(
        telegram_module.deliver_pending_phone_notifications(),
        telegram_module.deliver_pending_phone_notifications(),
    )
    assert sum(item["picked"] for item in results) == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_revision_change_after_send_keeps_new_revision_pending(
    sqlite_session_factory, monkeypatch: pytest.MonkeyPatch
):
    from app.models.entities import UserProfile
    from app.models.enums import CommunicationOutcome, PhoneSummaryState

    settings = Settings(
        _env_file=None,
        telegram_enabled=True,
        telegram_bot_token="bot-token",
        telegram_chat_id="chat-id",
    )
    monkeypatch.setattr(telegram_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
    target: list[object] = []
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            outcome=CommunicationOutcome.COMPLETED,
            summary_state=PhoneSummaryState.DONE,
            verification_revision=1,
            summary={"telegram": {"state": "pending", "input_revision": 1}},
        )
        db.add(call)
        await db.commit()
        target.append(call.id)

    async def send_and_change(**kwargs):
        async with sqlite_session_factory() as db:
            latest = await db.get(CommunicationSession, target[0])
            assert latest is not None
            latest.verification_revision = 2
            await db.commit()
        return TelegramDeliveryResult(message_id=101)

    monkeypatch.setattr(telegram_module, "send_telegram_message", send_and_change)
    result = await telegram_module.deliver_pending_phone_notifications()
    assert result["skipped"] == 1
    async with sqlite_session_factory() as db:
        stored = await db.get(CommunicationSession, target[0])
        assert stored is not None
        assert stored.summary["telegram"]["state"] == "pending"
        assert stored.summary["telegram"]["input_revision"] == 2
        assert stored.summary["telegram"]["message_id"] is None


@pytest.mark.asyncio
async def test_revision_change_on_error_path_never_writes_old_retry(
    sqlite_session_factory, monkeypatch: pytest.MonkeyPatch
):
    from app.models.entities import UserProfile
    from app.models.enums import CommunicationOutcome, PhoneSummaryState

    settings = Settings(
        _env_file=None,
        telegram_enabled=True,
        telegram_bot_token="bot-token",
        telegram_chat_id="chat-id",
    )
    monkeypatch.setattr(telegram_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        call = CommunicationSession(
            profile_id=profile.id,
            channel=CommunicationChannel.CALL,
            transport="phonegate",
            direction=CommunicationDirection.INBOUND,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            outcome=CommunicationOutcome.COMPLETED,
            summary_state=PhoneSummaryState.DONE,
            verification_revision=1,
            summary={"telegram": {"state": "pending", "input_revision": 1}},
        )
        db.add(call)
        await db.commit()
        call_id = call.id

    async def fail_and_change(**kwargs):
        async with sqlite_session_factory() as db:
            latest = await db.get(CommunicationSession, call_id)
            assert latest is not None
            latest.verification_revision = 2
            await db.commit()
        raise TelegramDeliveryError("http_503", retryable=True, permanent=False)

    monkeypatch.setattr(telegram_module, "send_telegram_message", fail_and_change)
    result = await telegram_module.deliver_pending_phone_notifications()
    assert result["skipped"] == 1
    async with sqlite_session_factory() as db:
        stored = await db.get(CommunicationSession, call_id)
        assert stored is not None
        assert stored.summary["telegram"]["state"] == "pending"
        assert stored.summary["telegram"]["input_revision"] == 2
        assert stored.summary["telegram"]["attempts"] == 0


@pytest.mark.asyncio
async def test_due_query_skips_terminal_and_not_due_rows_before_window(
    sqlite_session_factory, monkeypatch: pytest.MonkeyPatch
):
    from app.models.entities import UserProfile
    from app.models.enums import CommunicationOutcome, PhoneSummaryState

    settings = Settings(
        _env_file=None,
        telegram_enabled=True,
        telegram_bot_token="bot-token",
        telegram_chat_id="chat-id",
        phone_telegram_batch=1,
    )
    monkeypatch.setattr(telegram_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
    sent: list[str] = []

    async def fake_send(**kwargs):
        sent.append(kwargs["text"])
        return TelegramDeliveryResult(message_id=111)

    monkeypatch.setattr(telegram_module, "send_telegram_message", fake_send)
    now = datetime.now(UTC)
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="p", is_default=True)
        db.add(profile)
        await db.flush()
        rows = []
        for _i in range(8):
            rows.append(
                CommunicationSession(
                    profile_id=profile.id,
                    channel=CommunicationChannel.CALL,
                    transport="phonegate",
                    direction=CommunicationDirection.INBOUND,
                    started_at=now,
                    ended_at=now,
                    outcome=CommunicationOutcome.COMPLETED,
                    summary_state=PhoneSummaryState.DONE,
                    summary={"telegram": {"state": "sent", "input_revision": 0}},
                )
            )
        rows.append(
            CommunicationSession(
                profile_id=profile.id,
                channel=CommunicationChannel.CALL,
                transport="phonegate",
                direction=CommunicationDirection.INBOUND,
                started_at=now,
                ended_at=now + timedelta(seconds=1),
                outcome=CommunicationOutcome.COMPLETED,
                summary_state=PhoneSummaryState.DONE,
                summary={
                    "telegram": {
                        "state": "retrying",
                        "input_revision": 0,
                        "next_attempt_at": (now - timedelta(seconds=1)).isoformat(),
                    }
                },
            )
        )
        db.add_all(rows)
        await db.commit()
    result = await telegram_module.deliver_pending_phone_notifications()
    assert result["picked"] == 1
    assert result["sent"] == 1
    assert len(sent) == 1
