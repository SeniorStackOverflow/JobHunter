"""Strict, independent llmRouter passes used by post-call verification.

The provider deliberately keeps the Extractor and Verifier requests separate.  A
Verifier receives only the immutable call context; the Arbiter is the first pass
allowed to see another model's output.
"""

# ruff: noqa: RUF001 — Russian prompt text intentionally contains Cyrillic letters.

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.models.enums import CallFactState
from app.phone.critical import CriticalField

_Result = TypeVar("_Result", bound=BaseModel)


class VerificationTurn(BaseModel):
    """A transcript turn and its optional evidence quality metadata."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    # The persisted CommunicationTurn identity is separate from the display
    # path used to retrieve an audio clip.  Keeping both prevents a file path
    # from ever being mistaken for an evidence identity.
    turn_id: UUID
    speaker: str
    text: str
    asr_confidence: float | None = Field(default=None, ge=0, le=1)
    evidence_reference: str | None = None


class PersistedFact(BaseModel):
    """A fact already persisted for a call, supplied to the SMS comparator."""

    model_config = ConfigDict(extra="forbid")

    field: CriticalField
    raw_expression: str
    normalized_value: str | None = None
    state: CallFactState


class VerificationContext(BaseModel):
    """Typed source input shared by independent verification passes.

    The model is frozen to discourage accidental mutation after one pass has
    started.  Lists remain ordinary lists for ergonomic construction; callers
    treat this value as immutable and create a new context when source input
    changes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    call_started_at: datetime
    timezone: str
    transcript: list[VerificationTurn] = Field(default_factory=list)
    company: str | None = None
    vacancy: str | None = None
    application_status: str | None = None
    confirmed_facts: list[PersistedFact] = Field(default_factory=list)


class FactCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: CriticalField
    raw_expression: str = Field(
        description=(
            "The shortest exact transcript substring expressing this single field, not the "
            "whole multi-field sentence. Keep uncertainty, alternatives and negation intact."
        )
    )
    normalized_value: str | None = Field(
        description=(
            "Canonical value or null when ambiguous. Dates: YYYY-MM-DD; times: HH:MM; "
            "explicit timezones: IANA/UTC offset; format: onsite/remote/phone. For Russian "
            "address/company/vacancy preserve raw_expression spelling, case, inflection and "
            "words/digits, collapsing whitespace only; never translate or paraphrase."
        )
    )
    quote: str = Field(
        description=(
            "Non-empty exact bounded sentence or clause from the source employer turn, "
            "containing raw_expression and its relevant context."
        )
    )
    turn_seq: int | None
    confidence: float = Field(ge=0, le=1)
    ambiguity: str | None = None

    @model_validator(mode="after")
    def validate_canonical_shape(self) -> FactCandidate:
        """Reject values that cannot belong to the field before reconciliation."""
        value = self.normalized_value
        if value is None:
            return self
        valid = True
        if self.field == "interview_date":
            try:
                valid = date.fromisoformat(value).isoformat() == value
            except ValueError:
                valid = False
        elif self.field == "interview_time":
            valid = bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value))
        elif self.field == "meeting_url":
            parsed = urlsplit(value)
            valid = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        elif self.field == "format":
            valid = value in {"onsite", "remote", "phone"}
        if not valid:
            raise ValueError("normalized_value has an invalid canonical shape for field")
        return self


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_text: str
    outcome_guess: Literal[
        "interview_proposed",
        "callback_requested",
        "caller_will_retry",
        "info_request",
        "not_relevant",
        "unclear",
        "other",
    ]
    facts: list[FactCandidate]
    review_reasons: list[str]


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[FactCandidate]
    review_reasons: list[str]


class ArbitrationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: CriticalField
    accepted_value: str | None = Field(
        description=(
            "The agreed canonical value, or null when unsupported. Preserve Russian lexical "
            "values from raw_expression without translation, inflection changes or paraphrase."
        )
    )
    supporting_quote: str = Field(
        description="Exact bounded sentence or clause from the supporting employer turn."
    )
    accepted: bool
    reason: str


class ArbitrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[ArbitrationItem]


class SmsFieldComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: CriticalField
    relation: Literal["matches", "conflicts", "not_mentioned", "ambiguous"]
    sms_expression: str
    call_expression: str
    reason: str


class SmsComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comparisons: list[SmsFieldComparison]


@dataclass(frozen=True)
class ModelCallMeta:
    provider: str
    model: str
    latency_ms: int
    attempts: int
    validation_errors: tuple[tuple[str, tuple[str, ...]], ...] = ()


class VerificationUnavailable(RuntimeError):
    """A verification pass did not produce a usable structured response."""

    def __init__(
        self,
        reason: str,
        *,
        metadata: ModelCallMeta | None = None,
        validation_errors: tuple[tuple[str, tuple[str, ...]], ...] = (),
    ) -> None:
        # Keep this string intentionally code-only.  Provider exceptions may
        # contain prompts, phone numbers, SMS text, or credentials.
        self.reason = reason
        self.metadata = metadata
        self.validation_errors = validation_errors
        super().__init__(reason)


PhoneVerificationUnavailable = VerificationUnavailable

_SAFE_LOCATION_FIELDS = {
    "accepted",
    "accepted_value",
    "ambiguity",
    "application_status",
    "asr_confidence",
    "call_expression",
    "call_id",
    "call_started_at",
    "comparisons",
    "confidence",
    "confirmed_facts",
    "decisions",
    "evidence_reference",
    "facts",
    "field",
    "format",
    "interview_date",
    "interview_time",
    "meeting_url",
    "normalized_value",
    "outcome_guess",
    "quote",
    "raw_expression",
    "reason",
    "relation",
    "review_reasons",
    "sms_expression",
    "speaker",
    "state",
    "summary_text",
    "supporting_quote",
    "text",
    "timezone",
    "transcript",
    "turn_id",
    "turn_seq",
    "vacancy",
}


def _sanitize_validation_errors(
    errors: Sequence[object],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    safe: list[tuple[str, tuple[str, ...]]] = []
    for item in errors:
        if isinstance(item, dict):
            error_type = str(item.get("type", "validation_error"))[:64]
            raw_loc = item.get("loc", ())
        elif isinstance(item, tuple) and len(item) == 2:
            error_type, raw_loc = item
            error_type = str(error_type)[:64]
        else:
            continue
        if not isinstance(raw_loc, (list, tuple)):
            raw_loc = ()
        loc = tuple(
            part
            if isinstance(part, str) and part in _SAFE_LOCATION_FIELDS
            else "[index]"
            if isinstance(part, int)
            else "<field>"
            for part in raw_loc
        )[:8]
        value = (error_type, loc)
        if value not in safe:
            safe.append(value)
            if len(safe) == 16:
                break
    return tuple(safe)


def _strip_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().casefold() in {"```json", "```"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return text.strip()


def _safe_validation_errors(
    error: ValidationError,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Keep only bounded Pydantic error codes and locations."""

    return _sanitize_validation_errors(
        error.errors(include_url=False, include_context=False, include_input=False)
    )


def _nullable_schema(schema: dict[str, Any]) -> None:
    """Make a property nullable while preserving its validation constraints."""

    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        if not any(item == {"type": "null"} for item in schema["anyOf"]):
            schema["anyOf"].append({"type": "null"})
        schema.pop("default", None)
        return

    title = schema.get("title")
    inner = deepcopy(schema)
    inner.pop("default", None)
    inner.pop("title", None)
    schema.clear()
    if title is not None:
        schema["title"] = title
    schema["anyOf"] = [inner, {"type": "null"}]


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Pydantic schema for strict OpenAI-compatible providers."""

    normalized = deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                original_required = set(node.get("required", []))
                node["required"] = list(properties)
                for name, property_schema in properties.items():
                    if isinstance(property_schema, dict) and (
                        name not in original_required or "default" in property_schema
                    ):
                        _nullable_schema(property_schema)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(normalized)
    return normalized


_FIELD_CONTRACT = (
    " raw_expression — кратчайшая точная подстрока транскрипта для одного поля; "
    "не включайте соседние дату, время, адрес или часовой пояс. Не удаляйте слова "
    "неопределённости, отрицания и альтернативы: неоднозначное поле остаётся null с ambiguity. "
    "quote (у Арбитра supporting_quote) — точная непустая цитата одного предложения "
    "или части предложения с нужным контекстом из указанной реплики работодателя. "
    "normalized_value и accepted_value: дата YYYY-MM-DD, время HH:MM, явно названный "
    "часовой пояс IANA/UTC, format onsite/remote/phone только при прямом указании формата. "
    "Для address, company и vacancy сохраняйте русское написание raw_expression: "
    "не переводите, не перефразируйте, не меняйте регистр, падеж, слова или цифры; "
    "допустимо лишь убрать лишние пробелы. Например, из «По вакансии грузчика» "
    "raw_expression и normalized_value поля vacancy равны «грузчика». "
    "Из «Встреча 18 сентября в 09:45, улица Мира 7, по кишинёвскому времени» "
    "raw_expression даты = «18 сентября», времени = «09:45», адреса = «улица Мира 7», "
    "часового пояса = «по кишинёвскому времени»; цитата хранит контекст предложения. "
    "Примеры показывают форму ответа, а факты берите только из текущего транскрипта."
)
_EXTRACTOR_SYSTEM = (
    "Вы извлекаете проверяемые факты из записи телефонного разговора на русском языке. "
    "Верните только JSON по схеме. Сохраняйте исходные выражения и точные цитаты; "
    "не выдумывайте значения и указывайте неоднозначность. "
    "Для outcome_guess используйте callback_requested, только если работодатель просит "
    "кандидата перезвонить; caller_will_retry, только если работодатель сам говорит, что "
    "позвонит или перезвонит позже. Отсутствие даты, времени или деталей собеседования само "
    "по себе не является причиной review, если собеседование не предлагалось."
) + _FIELD_CONTRACT
_VERIFIER_SYSTEM = (
    "Вы независимо проверяете критические факты телефонного разговора на русском языке. "
    "Верните только JSON по схеме. Работайте исключительно с исходным контекстом и "
    "транскриптом; не предполагайте ответы другого проверяющего."
) + _FIELD_CONTRACT
_ARBITER_SYSTEM = (
    "Вы принимаете консервативное решение по двум независимым результатам проверки "
    "разговора на русском языке. Принимайте значение только при прямой однозначной "
    "цитате в транскрипте. Верните только JSON по схеме."
) + _FIELD_CONTRACT
_SMS_SYSTEM = (
    "Вы сравниваете SMS работодателя с уже сохранёнными фактами разговора. "
    "Верните только JSON по схеме, отмечая каждое поле как matches, conflicts, "
    "not_mentioned или ambiguous."
)


class PostCallVerificationProvider:
    """Run strict Extractor, Verifier, Arbiter, and SMS comparison passes."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "",
        extractor_model: str = "",
        verifier_model: str = "",
        arbiter_model: str = "",
        sms_model: str = "",
        prefer: str = "quality",
        timeout_seconds: float = 60.0,
        max_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        fallback = model.strip()
        configured = {
            "extractor": extractor_model.strip() or fallback,
            "verifier": verifier_model.strip() or fallback,
            "arbiter": arbiter_model.strip() or fallback,
            "sms": sms_model.strip() or fallback,
        }
        if not all(configured.values()):
            raise ValueError("model must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._models = configured
        self._prefer = prefer
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._client = client
        self._sleeper = sleeper or asyncio.sleep

    @property
    def models(self) -> dict[str, str]:
        return dict(self._models)

    @property
    def extractor_model(self) -> str:
        return self._models["extractor"]

    @property
    def verifier_model(self) -> str:
        return self._models["verifier"]

    @property
    def arbiter_model(self) -> str:
        return self._models["arbiter"]

    @property
    def sms_model(self) -> str:
        return self._models["sms"]

    def _context_text(self, ctx: VerificationContext) -> str:
        return json.dumps(ctx.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)

    def _body(
        self,
        pass_name: str,
        system: str,
        user: str,
        schema_model: type[BaseModel],
    ) -> dict[str, object]:
        return {
            "model": self._models[pass_name],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": 0,
            # Reasoning backends consume completion budget before emitting the
            # structured JSON; 4096 leaves enough room for the fact payload.
            "max_tokens": 4096,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": f"phone_verification_{pass_name}",
                    "strict": True,
                    "schema": _strict_json_schema(schema_model.model_json_schema()),
                },
            },
        }

    async def _complete_json(
        self,
        pass_name: str,
        system: str,
        user: str,
        schema_model: type[_Result],
    ) -> tuple[_Result, ModelCallMeta]:
        body = self._body(pass_name, system, user, schema_model)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-LLMRouter-Prefer": self._prefer,
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        started = time.perf_counter()
        attempts = 0
        terminal_reason: str | None = None
        validation_errors: tuple[tuple[str, tuple[str, ...]], ...] = ()
        try:
            while attempts < self._max_attempts:
                attempts += 1
                validation_errors = ()
                response: httpx.Response | None = None
                request_reason: str | None = None
                try:
                    response = await client.post(
                        f"{self._base_url}/v1/chat/completions",
                        headers=headers,
                        json=body,
                        follow_redirects=False,
                    )
                except httpx.TimeoutException:
                    request_reason = "timeout"
                except httpx.RequestError:
                    request_reason = "transport"

                if request_reason is not None:
                    terminal_reason = request_reason
                    if attempts < self._max_attempts:
                        await self._sleeper(min(4.0, 0.25 * (2 ** (attempts - 1))))
                        continue
                    break

                if response is None:
                    terminal_reason = "transport"
                    break
                if response.status_code >= 300:
                    terminal_reason = f"http_{response.status_code}"
                    if attempts < self._max_attempts and (
                        response.status_code == 429 or response.status_code >= 500
                    ):
                        await self._sleeper(min(4.0, 0.25 * (2 ** (attempts - 1))))
                        continue
                    break

                parse_reason: str | None = None
                content: object = None
                finish_reason: object = None
                try:
                    payload = response.json()
                    choice = payload["choices"][0]
                    content = choice["message"]["content"]
                    finish_reason = choice.get("finish_reason")
                except (ValueError, KeyError, IndexError, TypeError):
                    parse_reason = "malformed_envelope"
                if parse_reason is not None:
                    terminal_reason = parse_reason
                    if attempts < self._max_attempts:
                        continue
                    break
                if finish_reason == "length":
                    terminal_reason = "truncated"
                    if attempts < self._max_attempts:
                        continue
                    break
                if not isinstance(content, str) or not content.strip():
                    terminal_reason = "empty_content"
                    if attempts < self._max_attempts:
                        continue
                    break

                try:
                    result = schema_model.model_validate_json(_strip_fence(content))
                except ValidationError as exc:
                    terminal_reason = "schema_mismatch"
                    validation_errors = _safe_validation_errors(exc)
                    if attempts < self._max_attempts:
                        continue
                    break
                except ValueError:
                    terminal_reason = "schema_mismatch"
                    validation_errors = ()
                    if attempts < self._max_attempts:
                        continue
                    break

                latency = max(1, round((time.perf_counter() - started) * 1000))
                return result, ModelCallMeta(
                    "llmrouter", self._models[pass_name], latency, attempts
                )

            latency = max(1, round((time.perf_counter() - started) * 1000))
            metadata = ModelCallMeta(
                "llmrouter", self._models[pass_name], latency, attempts, validation_errors
            )
        finally:
            if owns_client:
                await client.aclose()

        # This raise is intentionally outside all exception handlers.  It keeps
        # transport and validation details out of __cause__, __context__, and
        # traceback rendering for callers that persist the failure.
        raise VerificationUnavailable(
            terminal_reason or "exhausted",
            metadata=metadata,
            validation_errors=validation_errors,
        )

    async def extract(self, ctx: VerificationContext) -> tuple[ExtractionResult, ModelCallMeta]:
        return await self._complete_json(
            "extractor", _EXTRACTOR_SYSTEM, self._context_text(ctx), ExtractionResult
        )

    async def verify(self, ctx: VerificationContext) -> tuple[VerificationResult, ModelCallMeta]:
        return await self._complete_json(
            "verifier", _VERIFIER_SYSTEM, self._context_text(ctx), VerificationResult
        )

    async def arbitrate(
        self,
        ctx: VerificationContext,
        extracted: ExtractionResult,
        verified: VerificationResult,
    ) -> tuple[ArbitrationResult, ModelCallMeta]:
        user = json.dumps(
            {
                "context": ctx.model_dump(mode="json"),
                "extractor_result": extracted.model_dump(mode="json"),
                "verifier_result": verified.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return await self._complete_json("arbiter", _ARBITER_SYSTEM, user, ArbitrationResult)

    async def compare_sms(
        self,
        ctx: VerificationContext,
        sms_text: str,
        facts: Sequence[PersistedFact],
    ) -> tuple[SmsComparisonResult, ModelCallMeta]:
        user = json.dumps(
            {
                "context": ctx.model_dump(mode="json"),
                "sms_text": sms_text,
                "facts": [fact.model_dump(mode="json") for fact in facts],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return await self._complete_json("sms", _SMS_SYSTEM, user, SmsComparisonResult)


__all__ = [
    "ArbitrationItem",
    "ArbitrationResult",
    "ExtractionResult",
    "FactCandidate",
    "ModelCallMeta",
    "PersistedFact",
    "PhoneVerificationUnavailable",
    "PostCallVerificationProvider",
    "SmsComparisonResult",
    "SmsFieldComparison",
    "VerificationContext",
    "VerificationResult",
    "VerificationTurn",
    "VerificationUnavailable",
]
