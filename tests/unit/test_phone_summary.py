import json

import httpx
import pytest

from app.phone.summary import (
    CallSummaryContext,
    PhoneSummaryProvider,
    PhoneSummaryUnavailable,
)

_CTX = CallSummaryContext(
    transcript=[
        ("assistant", "Здравствуйте"),
        ("employer", "Звоню по вакансии грузчика, в четверг в 14"),
    ],
    company="Example SRL",
    vacancy="Грузчик",
    application_status="sent",
    confirmed_facts={},
)


def _ok_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps(payload)}}
            ]
        },
    )


@pytest.mark.asyncio
async def test_summarize_parses_valid_json():
    payload = {
        "summary_text": "Работодатель предложил собеседование в четверг в 14:00.",
        "mentioned_vacancy": "Грузчик",
        "proposed_datetime_text": "в четверг в 14",
        "proposed_address_text": "",
        "contact_person_text": "",
        "outcome_guess": "interview_proposed",
        "needs_review": False,
    }
    seen = {}

    def handler(request):
        seen["prefer"] = request.headers.get("X-LLMRouter-Prefer")
        seen["url"] = str(request.url)
        return _ok_response(payload)

    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await p.summarize(_CTX)
    assert result.outcome_guess == "interview_proposed"
    assert result.proposed_datetime_text == "в четверг в 14"
    assert seen["prefer"] == "quality"
    assert seen["url"].endswith("/v1/chat/completions")


@pytest.mark.asyncio
async def test_summarize_rejects_non_json():
    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"finish_reason": "stop", "message": {"content": "sorry I cannot"}}
                        ]
                    },
                )
            )
        ),
    )
    with pytest.raises(PhoneSummaryUnavailable):
        await p.summarize(_CTX)


@pytest.mark.asyncio
async def test_summarize_maps_5xx_and_timeout():
    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(503))
        ),
    )
    with pytest.raises(PhoneSummaryUnavailable):
        await p.summarize(_CTX)


@pytest.mark.asyncio
async def test_summarize_strips_markdown_fence():
    fenced = "```json\n" + json.dumps({"summary_text": "ок"}) + "\n```"
    p = PhoneSummaryProvider(
        base_url="http://r",
        api_key="k",
        model="m",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"finish_reason": "stop", "message": {"content": fenced}}
                        ]
                    },
                )
            )
        ),
    )
    assert (await p.summarize(_CTX)).summary_text == "ок"
