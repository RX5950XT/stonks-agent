"""Fixed-origin OpenAI-compatible Chat Completions structured-output adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic, sleep
from typing import Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.adapters.llm._common import (
    ParsedProviderOutput,
    RawProviderResponse,
    RepairContext,
    complete_structured,
    invalid_provider_envelope,
)
from stonks_agent.adapters.llm._http import request_json, validate_api_key
from stonks_agent.adapters.llm._messages import provider_messages, schema_name
from stonks_agent.domain.errors import Result, Success
from stonks_agent.domain.model_policy import ModelPolicy, ModelProvider, ModelRoute
from stonks_agent.domain.research import StructuredLLMRequest, StructuredLLMResponse
from stonks_agent.ports.artifact_store import ArtifactStore


class _OpenAIMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    role: Literal["assistant"]
    content: str | None
    refusal: str | None = None


class _OpenAIChoice(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    index: Literal[0]
    message: _OpenAIMessage
    finish_reason: str


class _OpenAIPromptTokenDetails(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    cached_tokens: int = Field(default=0, ge=0)


class _OpenAIUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    prompt_tokens_details: _OpenAIPromptTokenDetails = Field(
        default_factory=lambda: _OpenAIPromptTokenDetails()
    )

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("provider token total does not match")
        if self.prompt_tokens_details.cached_tokens > self.prompt_tokens:
            raise ValueError("provider cache tokens exceed prompt tokens")
        return self


class _OpenAIEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    model: str = Field(min_length=1, max_length=256)
    choices: tuple[_OpenAIChoice, ...] = Field(min_length=1, max_length=1)
    usage: _OpenAIUsage


class OpenAICompatibleAdapter:
    __slots__ = (
        "_api_key",
        "_artifacts",
        "_client",
        "_clock",
        "_monotonic_clock",
        "_policy",
        "_route",
        "_sleeper",
    )

    def __init__(
        self,
        *,
        policy: ModelPolicy,
        request_model: str,
        client: httpx.Client,
        api_key: str,
        artifacts: ArtifactStore,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        validate_api_key(api_key)
        route = policy.resolve(request_model)
        if route.provider is not ModelProvider.OPENAI_COMPATIBLE:
            raise ValueError("OpenAI adapter requires an OpenAI provider route")
        self._policy = policy
        self._route = route
        self._client = client
        self._api_key = api_key
        self._artifacts = artifacts
        self._clock = clock or _utc_now
        self._monotonic_clock = monotonic_clock or monotonic
        self._sleeper = sleeper or sleep

    def complete(
        self,
        request: StructuredLLMRequest,
    ) -> Result[StructuredLLMResponse]:
        return complete_structured(
            request=request,
            policy=self._policy,
            expected_route=self._route,
            artifacts=self._artifacts,
            provider=self._provide,
            parser=_parse_openai,
            clock=self._clock,
        )

    def _provide(
        self,
        request: StructuredLLMRequest,
        repair: RepairContext | None,
    ) -> Result[RawProviderResponse]:
        return request_json(
            client=self._client,
            route=self._route,
            request=request,
            payload=_openai_payload(request, self._route, repair),
            headers={"Authorization": f"Bearer {self._api_key}"},
            clock=self._clock,
            monotonic_clock=self._monotonic_clock,
            sleeper=self._sleeper,
        )


def _openai_payload(
    request: StructuredLLMRequest,
    route: ModelRoute,
    repair: RepairContext | None,
) -> dict[str, object]:
    return {
        "model": route.provider_model,
        "messages": provider_messages(request, repair, include_system=True),
        "max_completion_tokens": request.max_output_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name(request),
                "strict": True,
                "schema": request.output_schema,
            },
        },
    }


def _parse_openai(raw: RawProviderResponse) -> Result[ParsedProviderOutput]:
    try:
        envelope = _OpenAIEnvelope.model_validate_json(raw.raw_body)
    except ValidationError:
        return invalid_provider_envelope()
    choice = envelope.choices[0]
    content = choice.message.content or "provider returned no structured content"
    terminal_reason: str | None = None
    repairable = True
    if choice.message.refusal is not None:
        terminal_reason = "refusal"
        repairable = False
    elif choice.finish_reason == "length":
        terminal_reason = "max_tokens"
    elif choice.finish_reason != "stop":
        terminal_reason = "unexpected_finish_reason"
        repairable = False
    return Success(
        ParsedProviderOutput(
            output_text=content,
            provider_model=envelope.model,
            input_tokens=envelope.usage.prompt_tokens,
            output_tokens=envelope.usage.completion_tokens,
            cached_input_tokens=envelope.usage.prompt_tokens_details.cached_tokens,
            terminal_reason=terminal_reason,
            terminal_repairable=repairable,
        )
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
