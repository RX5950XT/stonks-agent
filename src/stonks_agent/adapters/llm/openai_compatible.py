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
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.model_policy import ModelPolicy, ModelProvider, ModelRoute
from stonks_agent.domain.research import StructuredLLMRequest, StructuredLLMResponse
from stonks_agent.domain.secrets import ResolvedSecret, SecretAccessRequest, SecretRef
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_agent.ports.secret_provider import SecretProvider

_SECRET_PURPOSE = "openai_api_key"


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
        "_artifacts",
        "_client",
        "_clock",
        "_monotonic_clock",
        "_policy",
        "_route",
        "_secret_provider",
        "_secret_ref",
        "_sleeper",
    )

    def __init__(
        self,
        *,
        policy: ModelPolicy,
        request_model: str,
        client: httpx.Client,
        secret_provider: SecretProvider,
        secret_ref: SecretRef,
        artifacts: ArtifactStore,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        route = policy.resolve(request_model)
        if route.provider is not ModelProvider.OPENAI_COMPATIBLE:
            raise ValueError("OpenAI adapter requires an OpenAI provider route")
        if route.secret_ref != secret_ref:
            raise ValueError("OpenAI adapter secret reference does not match policy")
        self._policy = policy
        self._route = route
        self._client = client
        self._secret_provider = secret_provider
        self._secret_ref = secret_ref
        self._artifacts = artifacts
        self._clock = clock or _utc_now
        self._monotonic_clock = monotonic_clock or monotonic
        self._sleeper = sleeper or sleep

    def complete(
        self,
        request: StructuredLLMRequest,
    ) -> Result[StructuredLLMResponse]:
        credential: Result[ResolvedSecret] | None = None

        def provide(
            request: StructuredLLMRequest,
            repair: RepairContext | None,
        ) -> Result[RawProviderResponse]:
            nonlocal credential
            if credential is None:
                credential = self._resolve_credential()
            if isinstance(credential, Failure):
                return credential
            return self._provide(request, repair, credential.value.reveal())

        return complete_structured(
            request=request,
            policy=self._policy,
            expected_route=self._route,
            artifacts=self._artifacts,
            provider=provide,
            parser=_parse_openai,
            clock=self._clock,
        )

    def _provide(
        self,
        request: StructuredLLMRequest,
        repair: RepairContext | None,
        api_key: str,
    ) -> Result[RawProviderResponse]:
        return request_json(
            client=self._client,
            route=self._route,
            request=request,
            payload=_openai_payload(request, self._route, repair),
            headers={"Authorization": f"Bearer {api_key}"},
            clock=self._clock,
            monotonic_clock=self._monotonic_clock,
            sleeper=self._sleeper,
        )

    def _resolve_credential(self) -> Result[ResolvedSecret]:
        try:
            resolved = self._secret_provider.resolve(
                SecretAccessRequest(
                    reference=self._secret_ref,
                    purpose=_SECRET_PURPOSE,
                )
            )
            if isinstance(resolved, Failure):
                return _credential_failure(resolved.error.code)
            api_key = resolved.value.reveal()
            try:
                validate_api_key(api_key)
            except ValueError:
                return _credential_failure(ErrorCode.CONFIGURATION_INVALID)
            return resolved
        except Exception:
            return _credential_failure(ErrorCode.INTERNAL_ERROR)


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


def _credential_failure(source_code: ErrorCode) -> Failure:
    safe_code = (
        source_code
        if source_code in {ErrorCode.CONFIGURATION_INVALID, ErrorCode.DATA_UNAVAILABLE}
        else ErrorCode.INTERNAL_ERROR
    )
    return Failure(
        StructuredError(
            code=safe_code,
            message="Model provider credential is unavailable",
        )
    )
