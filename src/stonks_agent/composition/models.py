"""Fail-fast composition for one user-selected OpenAI-compatible model."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import cast

import httpx
from pydantic import BaseModel, ConfigDict, Field

from stonks_agent.adapters.llm.openai_compatible import OpenAICompatibleAdapter
from stonks_agent.adapters.security.ssrf import (
    ExactEndpoint,
    OutboundEndpointGuard,
    PinnedHTTPTransport,
    RuntimeEnvironment,
)
from stonks_agent.composition.secrets import (
    LLM_API_KEY_REF,
    build_llm_secret_provider,
)
from stonks_agent.domain.model_policy import (
    ModelPolicy,
    ModelProvider,
    ModelRoute,
)
from stonks_agent.ports.artifact_store import ArtifactStore

_REQUEST_MODEL = "policy:research-models-v1"


class LLMCompositionConfig(BaseModel):
    """Safe model metadata; the API key value is intentionally absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_environment: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    base_url: str = Field(min_length=1, max_length=512)
    endpoint: str = Field(
        default="/v1/chat/completions",
        min_length=1,
        max_length=256,
    )
    model_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$",
    )
    input_cost_per_million: Decimal = Field(default=Decimal("1.25"), ge=0)
    cached_input_cost_per_million: Decimal = Field(default=Decimal("0.625"), ge=0)
    cache_write_input_cost_per_million: Decimal = Field(default=Decimal("1.25"), ge=0)
    output_cost_per_million: Decimal = Field(default=Decimal("5"), ge=0)
    max_output_tokens: int = Field(default=4_096, ge=1, le=1_000_000)
    max_total_tokens: int = Field(default=32_768, ge=1, le=10_000_000)
    max_cost_usd: Decimal = Field(default=Decimal("1"), ge=0)
    max_transient_retries: int = Field(default=1, ge=0, le=3)
    max_repairs: int = Field(default=1, ge=0, le=2)
    timeout_seconds: Decimal = Field(default=Decimal("30"), gt=0, le=120)
    max_response_bytes: int = Field(
        default=1_048_576,
        ge=1,
        le=16_777_216,
    )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        runtime_environment: str,
    ) -> LLMCompositionConfig:
        """Read only non-secret route metadata from an injected environment."""

        return cls(
            runtime_environment=runtime_environment,
            base_url=_required(environment, "STONKS_LLM_BASE_URL"),
            endpoint=environment.get("STONKS_LLM_ENDPOINT", "/v1/chat/completions"),
            model_id=_required(environment, "STONKS_LLM_MODEL"),
            input_cost_per_million=_decimal(
                environment, "STONKS_LLM_INPUT_COST_PER_MILLION", "1.25"
            ),
            cached_input_cost_per_million=_decimal(
                environment,
                "STONKS_LLM_CACHED_INPUT_COST_PER_MILLION",
                "0.625",
            ),
            cache_write_input_cost_per_million=_decimal(
                environment,
                "STONKS_LLM_CACHE_WRITE_INPUT_COST_PER_MILLION",
                "1.25",
            ),
            output_cost_per_million=_decimal(
                environment, "STONKS_LLM_OUTPUT_COST_PER_MILLION", "5"
            ),
            max_output_tokens=_integer(
                environment, "STONKS_LLM_MAX_OUTPUT_TOKENS", 4_096
            ),
            max_total_tokens=_integer(
                environment, "STONKS_LLM_MAX_TOTAL_TOKENS", 32_768
            ),
            max_cost_usd=_decimal(environment, "STONKS_LLM_MAX_COST_USD", "1"),
            max_transient_retries=_integer(
                environment, "STONKS_LLM_MAX_TRANSIENT_RETRIES", 1
            ),
            max_repairs=_integer(environment, "STONKS_LLM_MAX_REPAIRS", 1),
            timeout_seconds=_decimal(environment, "STONKS_LLM_TIMEOUT_SECONDS", "30"),
            max_response_bytes=_integer(
                environment, "STONKS_LLM_MAX_RESPONSE_BYTES", 1_048_576
            ),
        )


def build_model_policy(config: LLMCompositionConfig) -> ModelPolicy:
    return ModelPolicy(
        policy_id="research-models-v1",
        routes=(
            ModelRoute(
                request_model=_REQUEST_MODEL,
                provider=ModelProvider.OPENAI_COMPATIBLE,
                provider_model=config.model_id,
                environment=config.runtime_environment,
                origin=config.base_url,
                endpoint=config.endpoint,
                secret_ref=LLM_API_KEY_REF,
                input_cost_per_million=config.input_cost_per_million,
                cached_input_cost_per_million=config.cached_input_cost_per_million,
                cache_write_input_cost_per_million=(
                    config.cache_write_input_cost_per_million
                ),
                output_cost_per_million=config.output_cost_per_million,
                max_output_tokens=config.max_output_tokens,
                max_total_tokens_per_request=config.max_total_tokens,
                max_cost_usd_per_request=config.max_cost_usd,
                max_transient_retries=config.max_transient_retries,
                max_repairs=config.max_repairs,
                timeout_seconds=config.timeout_seconds,
                max_response_bytes=config.max_response_bytes,
            ),
        ),
    )


def build_llm(
    config: LLMCompositionConfig,
    *,
    environment: Mapping[str, str],
    artifacts: ArtifactStore,
    client: httpx.Client,
) -> OpenAICompatibleAdapter:
    policy = build_model_policy(config)
    return OpenAICompatibleAdapter(
        policy=policy,
        request_model=_REQUEST_MODEL,
        client=client,
        secret_provider=build_llm_secret_provider(
            runtime_environment=config.runtime_environment,
            environment=environment,
        ),
        secret_ref=LLM_API_KEY_REF,
        artifacts=artifacts,
    )


def build_model_http_client(config: LLMCompositionConfig) -> httpx.Client:
    """Create an exact, DNS-pinned client for one configured model route."""

    allowed_environments = {
        "local",
        "development",
        "test",
        "staging",
        "production",
    }
    if config.runtime_environment not in allowed_environments:
        raise ValueError("LLM runtime environment is invalid")
    endpoint = ExactEndpoint.from_url(
        f"{config.base_url}{config.endpoint}",
        environment=cast(RuntimeEnvironment, config.runtime_environment),
    )
    guard = OutboundEndpointGuard(endpoint)
    return httpx.Client(
        transport=PinnedHTTPTransport(guard),
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(float(config.timeout_seconds)),
    )


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value or value.strip() != value:
        raise ValueError("LLM configuration is invalid")
    return value


def _integer(environment: Mapping[str, str], name: str, default: int) -> int:
    raw = environment.get(name)
    if raw is None:
        return default
    try:
        if (
            not raw
            or raw.strip() != raw
            or any(character not in "0123456789" for character in raw)
        ):
            raise ValueError
        return int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("LLM configuration is invalid") from error


def _decimal(
    environment: Mapping[str, str],
    name: str,
    default: str,
) -> Decimal:
    raw = environment.get(name, default)
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("LLM configuration is invalid") from error
    if not value.is_finite():
        raise ValueError("LLM configuration is invalid")
    return value
