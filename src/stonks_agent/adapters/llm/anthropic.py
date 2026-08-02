"""Fixed-origin Anthropic Messages structured-output adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from time import monotonic, sleep
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.adapters.llm._common import (
    ParsedProviderOutput,
    RawProviderResponse,
    RepairContext,
    complete_structured,
    invalid_provider_envelope,
)
from stonks_agent.adapters.llm._http import (
    reject_secret_echo,
    request_json,
    resolve_api_credential,
)
from stonks_agent.adapters.llm._messages import provider_messages, system_text
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.errors import (
    Failure,
    Result,
    Success,
)
from stonks_agent.domain.model_policy import ModelPolicy, ModelProvider, ModelRoute
from stonks_agent.domain.research import StructuredLLMRequest, StructuredLLMResponse
from stonks_agent.domain.secrets import ResolvedSecret, SecretRef
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_agent.ports.secret_provider import SecretProvider

_SECRET_PURPOSE = "anthropic_api_key"


class _AnthropicText(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["text"]
    text: str = Field(min_length=1, max_length=1_048_576)


class _AnthropicUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)

    @property
    def total_input_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


class _AnthropicEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["message"]
    role: Literal["assistant"]
    model: str = Field(min_length=1, max_length=256)
    content: tuple[_AnthropicText, ...] = Field(min_length=1, max_length=1)
    stop_reason: str
    usage: _AnthropicUsage


class AnthropicAdapter:
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
        if route.provider is not ModelProvider.ANTHROPIC:
            raise ValueError("Anthropic adapter requires an Anthropic provider route")
        if route.secret_ref != secret_ref:
            raise ValueError("Anthropic adapter secret reference does not match policy")
        self._policy = policy
        self._route = route
        self._client = client
        self._secret_provider = secret_provider
        self._secret_ref = secret_ref
        self._artifacts = artifacts
        self._clock = clock or utc_now
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
            parser=_parse_anthropic,
            clock=self._clock,
        )

    def _provide(
        self,
        request: StructuredLLMRequest,
        repair: RepairContext | None,
        api_key: str,
    ) -> Result[RawProviderResponse]:
        return reject_secret_echo(
            request_json(
                client=self._client,
                route=self._route,
                request=request,
                payload=_anthropic_payload(request, self._route, repair),
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                clock=self._clock,
                monotonic_clock=self._monotonic_clock,
                sleeper=self._sleeper,
            ),
            secret=api_key,
        )

    def _resolve_credential(self) -> Result[ResolvedSecret]:
        return resolve_api_credential(
            provider=self._secret_provider,
            reference=self._secret_ref,
            purpose=_SECRET_PURPOSE,
        )


def _anthropic_payload(
    request: StructuredLLMRequest,
    route: ModelRoute,
    repair: RepairContext | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": route.provider_model,
        "messages": provider_messages(request, repair, include_system=False),
        "max_tokens": request.max_output_tokens,
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": request.output_schema,
            }
        },
    }
    system = system_text(request)
    if system is not None:
        payload["system"] = system
    return payload


def _parse_anthropic(raw: RawProviderResponse) -> Result[ParsedProviderOutput]:
    try:
        envelope = _AnthropicEnvelope.model_validate_json(raw.raw_body)
    except ValidationError:
        return invalid_provider_envelope()
    terminal_reason: str | None = None
    repairable = True
    if envelope.stop_reason == "refusal":
        terminal_reason = "refusal"
        repairable = False
    elif envelope.stop_reason == "max_tokens":
        terminal_reason = "max_tokens"
    elif envelope.stop_reason != "end_turn":
        terminal_reason = "unexpected_stop_reason"
        repairable = False
    return Success(
        ParsedProviderOutput(
            output_text=envelope.content[0].text,
            provider_model=envelope.model,
            input_tokens=envelope.usage.total_input_tokens,
            output_tokens=envelope.usage.output_tokens,
            cached_input_tokens=envelope.usage.cache_read_input_tokens,
            cache_write_input_tokens=envelope.usage.cache_creation_input_tokens,
            terminal_reason=terminal_reason,
            terminal_repairable=repairable,
        )
    )
