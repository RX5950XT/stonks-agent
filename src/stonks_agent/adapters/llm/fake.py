"""Deterministic offline structured-output adapter for tests and replay."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from threading import RLock

from pydantic import BaseModel, ConfigDict

from stonks_agent.adapters.llm._common import (
    ParsedProviderOutput,
    RawProviderResponse,
    RepairContext,
    complete_structured,
    invalid_provider_envelope,
    resolve_route,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.model_policy import ModelPolicy, ModelProvider
from stonks_agent.domain.research import StructuredLLMRequest, StructuredLLMResponse
from stonks_agent.domain.usage_budget import UsageConsumption
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_contracts.common import canonical_json


class FakeLLMOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parsed_output: dict[str, object]
    usage: UsageConsumption


class FakeStructuredLLMAdapter:
    __slots__ = ("_artifacts", "_clock", "_lock", "_outputs", "_policy")

    def __init__(
        self,
        *,
        policy: ModelPolicy,
        artifacts: ArtifactStore,
        outputs: Iterable[FakeLLMOutput] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._artifacts = artifacts
        self._outputs = deque(outputs)
        self._clock = clock or _utc_now
        self._lock = RLock()

    @property
    def remaining_outputs(self) -> int:
        with self._lock:
            return len(self._outputs)

    def complete(
        self,
        request: StructuredLLMRequest,
    ) -> Result[StructuredLLMResponse]:
        return complete_structured(
            request=request,
            policy=self._policy,
            expected_route=None,
            artifacts=self._artifacts,
            provider=self._provide,
            parser=self._parse,
            clock=self._clock,
        )

    def _provide(
        self,
        request: StructuredLLMRequest,
        repair: RepairContext | None,
    ) -> Result[RawProviderResponse]:
        del repair
        route = resolve_route(self._policy, request.model)
        if isinstance(route, Failure) or route.value.provider is not ModelProvider.FAKE:
            return Failure(
                StructuredError(
                    code=ErrorCode.CONFIGURATION_INVALID,
                    message="Fake model route is not allowlisted",
                )
            )
        with self._lock:
            if not self._outputs:
                return Failure(
                    StructuredError(
                        code=ErrorCode.DATA_UNAVAILABLE,
                        message="Fake model output script is exhausted",
                    )
                )
            scripted = self._outputs.popleft()
        raw = canonical_json(scripted.parsed_output).encode("utf-8")
        return Success(
            RawProviderResponse(
                raw_body=raw,
                elapsed_ms=scripted.usage.elapsed_ms,
                created_at=self._clock(),
                provider_model_hint=route.value.provider_model,
                input_tokens_hint=scripted.usage.input_tokens,
                output_tokens_hint=scripted.usage.output_tokens,
            )
        )

    @staticmethod
    def _parse(raw: RawProviderResponse) -> Result[ParsedProviderOutput]:
        if (
            raw.provider_model_hint is None
            or raw.input_tokens_hint is None
            or raw.output_tokens_hint is None
        ):
            return invalid_provider_envelope()
        return Success(
            ParsedProviderOutput(
                output_text=raw.raw_body.decode("utf-8"),
                provider_model=raw.provider_model_hint,
                input_tokens=raw.input_tokens_hint,
                output_tokens=raw.output_tokens_hint,
            )
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)
