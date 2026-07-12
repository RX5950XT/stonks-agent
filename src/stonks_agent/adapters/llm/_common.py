"""Provider-neutral structured-output validation, archival, and accounting."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, Self

from jsonschema import exceptions, validators
from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.model_policy import ModelPolicy, ModelRoute
from stonks_agent.domain.research import StructuredLLMRequest, StructuredLLMResponse
from stonks_agent.domain.usage_budget import UsageConsumption
from stonks_agent.ports.artifact_store import ArtifactManifest, ArtifactStore
from stonks_contracts.common import UTCDateTime
from stonks_contracts.evidence import Sensitivity


class RawProviderResponse(BaseModel):
    """Exact provider bytes plus local transport facts, before envelope parsing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_body: bytes = Field(min_length=1)
    elapsed_ms: int = Field(ge=0, le=86_400_000)
    created_at: UTCDateTime
    provider_model_hint: str | None = Field(default=None, max_length=256)
    input_tokens_hint: int | None = Field(default=None, ge=0, le=10_000_000)
    output_tokens_hint: int | None = Field(default=None, ge=0, le=1_000_000)


class ParsedProviderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    output_text: str = Field(min_length=1, max_length=1_048_576)
    provider_model: str = Field(min_length=1, max_length=256)
    input_tokens: int = Field(ge=0, le=10_000_000)
    output_tokens: int = Field(ge=0, le=1_000_000)
    cached_input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    cache_write_input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    terminal_reason: str | None = Field(default=None, max_length=128)
    terminal_repairable: bool = True

    @model_validator(mode="after")
    def validate_cache_token_details(self) -> Self:
        if self.cached_input_tokens + self.cache_write_input_tokens > self.input_tokens:
            raise ValueError("cache token details exceed total input tokens")
        return self


@dataclass(frozen=True, slots=True)
class RepairContext:
    prior_output: str
    reason: str


class OutputProvider(Protocol):
    def __call__(
        self,
        request: StructuredLLMRequest,
        repair: RepairContext | None,
    ) -> Result[RawProviderResponse]: ...


class OutputParser(Protocol):
    def __call__(
        self,
        raw: RawProviderResponse,
    ) -> Result[ParsedProviderOutput]: ...


def complete_structured(
    *,
    request: StructuredLLMRequest,
    policy: ModelPolicy,
    expected_route: ModelRoute | None,
    artifacts: ArtifactStore,
    provider: OutputProvider,
    parser: OutputParser,
    clock: Callable[[], datetime],
) -> Result[StructuredLLMResponse]:
    prepared = _prepare(request, policy, expected_route, clock)
    if isinstance(prepared, Failure):
        return prepared
    route = prepared.value
    usage = UsageConsumption()
    repair: RepairContext | None = None
    parsed_result: Result[dict[str, object]] = _invalid_output("no_output")
    for attempt in range(route.max_repairs + 1):
        if _deadline_passed(request, clock):
            return _with_usage(_deadline_failure(), usage, attempt)
        outcome = provider(request, repair)
        if isinstance(outcome, Failure):
            return _with_usage(outcome, usage, attempt)
        raw = outcome.value
        archived = _archive(raw, route, artifacts)
        if isinstance(archived, Failure):
            return _with_usage(archived, usage, attempt + 1)
        parsed_provider = parser(raw)
        if isinstance(parsed_provider, Failure):
            return _with_usage(parsed_provider, usage, attempt + 1)
        output = parsed_provider.value
        usage = _add_usage(usage, raw, output, route)
        exceeded = _usage_exceeded(usage, route)
        if exceeded:
            return _with_usage(_budget_failure(exceeded), usage, attempt + 1)
        if _deadline_passed(request, clock):
            return _with_usage(_deadline_failure(), usage, attempt + 1)
        parsed_result = _parse_and_validate(output, request, route)
        if isinstance(parsed_result, Success):
            return Success(
                StructuredLLMResponse(
                    request_id=request.request_id,
                    model=output.provider_model,
                    parsed_output=parsed_result.value,
                    raw_output_artifact_ref=f"sha256:{archived.value.content_hash}",
                    usage=usage,
                    created_at=raw.created_at,
                )
            )
        if parsed_result.error.details.get("repairable") is False:
            return _with_usage(parsed_result, usage, attempt + 1)
        repair = RepairContext(
            prior_output=output.output_text,
            reason=str(parsed_result.error.details.get("reason", "invalid_output")),
        )
    assert isinstance(parsed_result, Failure)
    return _with_usage(parsed_result, usage, route.max_repairs + 1)


def resolve_route(
    policy: ModelPolicy,
    request_model: str,
) -> Result[ModelRoute]:
    try:
        return Success(policy.resolve(request_model))
    except LookupError:
        return Failure(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="Requested model is not allowlisted",
            )
        )


def invalid_provider_envelope() -> Failure:
    return _invalid_output("provider_envelope_invalid", repairable=False)


def _prepare(
    request: StructuredLLMRequest,
    policy: ModelPolicy,
    expected_route: ModelRoute | None,
    clock: Callable[[], datetime],
) -> Result[ModelRoute]:
    resolved = resolve_route(policy, request.model)
    if isinstance(resolved, Failure):
        return resolved
    route = resolved.value
    if expected_route is not None and route != expected_route:
        return Failure(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="Model adapter route does not match request allowlist",
            )
        )
    if request.max_output_tokens > route.max_output_tokens:
        return _budget_failure(("max_output_tokens",))
    if _deadline_passed(request, clock):
        return _deadline_failure()
    try:
        validator = validators.validator_for(request.output_schema)
        validator.check_schema(request.output_schema)
    except exceptions.SchemaError:
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Structured output schema is invalid",
            )
        )
    return Success(route)


def _parse_and_validate(
    output: ParsedProviderOutput,
    request: StructuredLLMRequest,
    route: ModelRoute,
) -> Result[dict[str, object]]:
    if output.provider_model != route.provider_model:
        return _invalid_output("provider_model_mismatch", repairable=False)
    if output.terminal_reason is not None:
        return _invalid_output(
            output.terminal_reason,
            repairable=output.terminal_repairable,
        )
    try:
        value = json.loads(output.output_text)
    except (json.JSONDecodeError, UnicodeError):
        return _invalid_output("invalid_json")
    if not isinstance(value, dict):
        return _invalid_output("root_not_object")
    try:
        validator = validators.validator_for(request.output_schema)
        validator(request.output_schema).validate(value)
    except exceptions.ValidationError:
        return _invalid_output("schema_validation_failed")
    return Success(value)


def _archive(
    raw: RawProviderResponse,
    route: ModelRoute,
    artifacts: ArtifactStore,
) -> Result[ArtifactManifest]:
    result = artifacts.finalize(
        raw.raw_body,
        metadata=ArtifactMetadata(
            media_type="application/json",
            license_tag="provider-output-contract",
            sensitivity=Sensitivity.INTERNAL,
            source=f"llm:{route.provider.value}",
            attributes=(("expected_provider_model", route.provider_model),),
        ),
        finalized_at=raw.created_at,
    )
    if isinstance(result, Failure):
        return Failure(
            StructuredError(
                code=ErrorCode.INTERNAL_ERROR,
                message="Model output artifact could not be finalized",
            )
        )
    return result


def _add_usage(
    current: UsageConsumption,
    raw: RawProviderResponse,
    output: ParsedProviderOutput,
    route: ModelRoute,
) -> UsageConsumption:
    standard_input_tokens = (
        output.input_tokens
        - output.cached_input_tokens
        - output.cache_write_input_tokens
    )
    input_cost = Decimal(standard_input_tokens) * route.input_cost_per_million
    cached_cost = (
        Decimal(output.cached_input_tokens) * route.cached_input_cost_per_million
    )
    cache_write_cost = (
        Decimal(output.cache_write_input_tokens)
        * route.cache_write_input_cost_per_million
    )
    output_cost = Decimal(output.output_tokens) * route.output_cost_per_million
    return UsageConsumption(
        input_tokens=current.input_tokens + output.input_tokens,
        output_tokens=current.output_tokens + output.output_tokens,
        cost_usd=current.cost_usd
        + (input_cost + cached_cost + cache_write_cost + output_cost)
        / Decimal(1_000_000),
        elapsed_ms=current.elapsed_ms + raw.elapsed_ms,
    )


def _usage_exceeded(
    usage: UsageConsumption,
    route: ModelRoute,
) -> tuple[str, ...]:
    checks = (
        ("total_tokens", usage.total_tokens > route.max_total_tokens_per_request),
        ("cost_usd", usage.cost_usd > route.max_cost_usd_per_request),
    )
    return tuple(name for name, exceeded in checks if exceeded)


def _deadline_passed(
    request: StructuredLLMRequest,
    clock: Callable[[], datetime],
) -> bool:
    now = clock()
    return now.tzinfo is None or now >= request.deadline_at


def _with_usage(failure: Failure, usage: UsageConsumption, attempts: int) -> Failure:
    details = dict(failure.error.details)
    details.update(attempts=attempts, usage=usage.model_dump(mode="json"))
    return Failure(
        StructuredError(
            code=failure.error.code,
            message=failure.error.message,
            details=details,
        )
    )


def _invalid_output(reason: str, *, repairable: bool = True) -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.MODEL_OUTPUT_INVALID,
            message="Structured model output is invalid",
            details={"reason": reason, "repairable": repairable},
        )
    )


def _budget_failure(exceeded: tuple[str, ...]) -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.BUDGET_EXHAUSTED,
            message="Model request budget exhausted",
            details={"exceeded": exceeded},
        )
    )


def _deadline_failure() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.DEADLINE_EXCEEDED,
            message="Model request deadline exceeded",
        )
    )
