"""Strict, independent llmRouter passes used by post-call verification.

The provider deliberately keeps the Extractor and Verifier requests separate.  A
Verifier receives only the immutable call context; the Arbiter is the first pass
allowed to see another model's output.
"""

# ruff: noqa: RUF001 — Russian prompt text intentionally contains Cyrillic letters.

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models.enums import CallFactState
from app.phone.critical import CriticalField

_Result = TypeVar("_Result", bound=BaseModel)


class VerificationTurn(BaseModel):
    """A transcript turn and its optional evidence quality metadata."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
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
    raw_expression: str
    normalized_value: str | None
    quote: str
    turn_seq: int | None
    confidence: float = Field(ge=0, le=1)
    ambiguity: str = ""


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_text: str
    outcome_guess: Literal["interview_proposed", "info_request", "not_relevant", "unclear", "other"]
    facts: list[FactCandidate]
    review_reasons: list[str]


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[FactCandidate]
    review_reasons: list[str]


class ArbitrationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: CriticalField
    accepted_value: str | None
    supporting_quote: str
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


class VerificationUnavailable(RuntimeError):
    """A verification pass did not produce a usable structured response."""

    def __init__(self, reason: str, *, metadata: ModelCallMeta | None = None) -> None:
        # Keep this string intentionally code-only.  Provider exceptions may
        # contain prompts, phone numbers, SMS text, or credentials.
        self.reason = reason
        self.metadata = metadata
        super().__init__(reason)


PhoneVerificationUnavailable = VerificationUnavailable


def _strip_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().casefold() in {"```json", "```"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return text.strip()


_EXTRACTOR_SYSTEM = (
    "Вы извлекаете проверяемые факты из записи телефонного разговора на русском языке. "
    "Верните только JSON по схеме. Сохраняйте исходные выражения и точные цитаты; "
    "не выдумывайте значения и указывайте неоднозначность."
)
_VERIFIER_SYSTEM = (
    "Вы независимо проверяете критические факты телефонного разговора на русском языке. "
    "Верните только JSON по схеме. Работайте исключительно с исходным контекстом и "
    "транскриптом; не предполагайте ответы другого проверяющего."
)
_ARBITER_SYSTEM = (
    "Вы принимаете консервативное решение по двум независимым результатам проверки "
    "разговора на русском языке. Принимайте значение только при прямой однозначной "
    "цитате в транскрипте. Верните только JSON по схеме."
)
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
            "max_tokens": 1200,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": f"phone_verification_{pass_name}",
                    "strict": True,
                    "schema": schema_model.model_json_schema(),
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
        try:
            while attempts < self._max_attempts:
                attempts += 1
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
                        continue
                    break

                parse_reason: str | None = None
                content: object = None
                try:
                    payload = response.json()
                    content = payload["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError):
                    parse_reason = "malformed_envelope"
                if parse_reason is not None:
                    terminal_reason = parse_reason
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
                except (ValidationError, ValueError):
                    terminal_reason = "schema_mismatch"
                    if attempts < self._max_attempts:
                        continue
                    break

                latency = max(1, round((time.perf_counter() - started) * 1000))
                return result, ModelCallMeta(
                    "llmrouter", self._models[pass_name], latency, attempts
                )

            latency = max(1, round((time.perf_counter() - started) * 1000))
            metadata = ModelCallMeta("llmrouter", self._models[pass_name], latency, attempts)
        finally:
            if owns_client:
                await client.aclose()

        # This raise is intentionally outside all exception handlers.  It keeps
        # transport and validation details out of __cause__, __context__, and
        # traceback rendering for callers that persist the failure.
        raise VerificationUnavailable(terminal_reason or "exhausted", metadata=metadata)

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
