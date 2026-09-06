from __future__ import annotations

import json
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ValidationError

logger = structlog.get_logger(__name__)

_SYSTEM = (
    "You summarize a finished phone call between an employer and a job candidate's "
    "voice assistant. Reply with ONE JSON object and nothing else. Write summary_text "
    "in Russian, 2-4 sentences. Copy the caller's wording for dates, times and "
    "addresses into the *_text fields — do NOT normalize them. Never invent facts about "
    "the candidate. Set needs_review=true if the outcome, date, time or address is "
    "unclear or contradictory."
)


class CallSummary(BaseModel):
    summary_text: str
    mentioned_vacancy: str = ""
    proposed_datetime_text: str = ""
    proposed_address_text: str = ""
    contact_person_text: str = ""
    outcome_guess: Literal[
        "interview_proposed", "info_request", "not_relevant", "unclear", "other"
    ] = "unclear"
    needs_review: bool = False


class CallSummaryContext(BaseModel):
    transcript: list[tuple[str, str]]
    company: str | None = None
    vacancy: str | None = None
    application_status: str | None = None
    confirmed_facts: dict[str, Any] = {}


class PhoneSummaryUnavailable(RuntimeError):
    """The summary model did not return a usable result."""


def _strip_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().casefold() in {"```json", "```"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return text.strip()


class PhoneSummaryProvider:
    def __init__(
        self, *, base_url: str, api_key: str, model: str,
        prefer: str = "quality", timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model.strip()
        self._prefer = prefer
        self._timeout = timeout_seconds
        self._client = client

    def _body(self, ctx: CallSummaryContext) -> dict[str, Any]:
        header = []
        if ctx.company:
            header.append(f"Компания: {ctx.company}")
        if ctx.vacancy:
            header.append(f"Вакансия: {ctx.vacancy}")
        if ctx.application_status:
            header.append(f"Статус отклика: {ctx.application_status}")
        lines = [f"{who}: {text}" for who, text in ctx.transcript]
        user = "\n".join([*header, "", "Транскрипт:", *lines])
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": 700,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "call_summary", "strict": True,
                                "schema": CallSummary.model_json_schema()},
            },
        }

    async def summarize(self, ctx: CallSummaryContext) -> CallSummary:
        if self._client is not None:
            return await self._summarize_with(self._client, ctx)
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
            return await self._summarize_with(client, ctx)

    async def _summarize_with(
        self, client: httpx.AsyncClient, ctx: CallSummaryContext
    ) -> CallSummary:
        headers = {"Authorization": f"Bearer {self._api_key}", "X-LLMRouter-Prefer": self._prefer}
        try:
            response = await client.post(
                f"{self._base_url}/v1/chat/completions", headers=headers, json=self._body(ctx)
            )
        except httpx.RequestError as exc:
            raise PhoneSummaryUnavailable(f"transport:{type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise PhoneSummaryUnavailable(f"http_{response.status_code}")
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise PhoneSummaryUnavailable("malformed_envelope") from exc
        if not isinstance(content, str) or not content.strip():
            raise PhoneSummaryUnavailable("empty_content")
        try:
            return CallSummary.model_validate_json(_strip_fence(content))
        except ValidationError as exc:
            raise PhoneSummaryUnavailable("schema_mismatch") from exc
