from __future__ import annotations

from decimal import Decimal

import httpx

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.llm.openai_compatible import OpenAICompatibleAdapter
from stonks_agent.composition.models import (
    LLMCompositionConfig,
    build_llm,
    build_model_policy,
)
from stonks_agent.domain.model_policy import ModelProvider


def environment(**overrides: str) -> dict[str, str]:
    values = {
        "STONKS_LLM_BASE_URL": "http://127.0.0.1:11434",
        "STONKS_LLM_MODEL": "local-model",
        "STONKS_LLM_API_KEY": "not-a-real-key",
    }
    values.update(overrides)
    return values


def test_environment_composition_builds_a_bounded_openai_compatible_route() -> None:
    config = LLMCompositionConfig.from_environment(
        environment(),
        runtime_environment="local",
    )

    policy = build_model_policy(config)
    route = policy.resolve("policy:research-models-v1")

    assert policy.policy_id == "research-models-v1"
    assert route.provider is ModelProvider.OPENAI_COMPATIBLE
    assert route.origin == "http://127.0.0.1:11434"
    assert route.endpoint == "/v1/chat/completions"
    assert route.provider_model == "local-model"
    assert route.max_output_tokens == 4_096
    assert route.max_cost_usd_per_request == Decimal("1")
    assert "not-a-real-key" not in repr(config)
    assert "not-a-real-key" not in repr(policy)


def test_cost_and_limit_overrides_are_strictly_parsed() -> None:
    config = LLMCompositionConfig.from_environment(
        environment(
            STONKS_LLM_MAX_OUTPUT_TOKENS="2048",
            STONKS_LLM_MAX_TOTAL_TOKENS="8192",
            STONKS_LLM_MAX_COST_USD="0.25",
            STONKS_LLM_TIMEOUT_SECONDS="30",
        ),
        runtime_environment="development",
    )

    route = build_model_policy(config).routes[0]

    assert route.max_output_tokens == 2_048
    assert route.max_total_tokens_per_request == 8_192
    assert route.max_cost_usd_per_request == Decimal("0.25")
    assert route.timeout_seconds == Decimal("30")


def test_build_llm_uses_injected_secret_provider_without_persisting_key() -> None:
    values = environment()
    config = LLMCompositionConfig.from_environment(
        values,
        runtime_environment="local",
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        trust_env=False,
    )
    try:
        llm = build_llm(
            config,
            environment=values,
            artifacts=MemoryArtifactStore(),
            client=client,
        )
    finally:
        client.close()

    assert isinstance(llm, OpenAICompatibleAdapter)
    assert "not-a-real-key" not in repr(llm)
