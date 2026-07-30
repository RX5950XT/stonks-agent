"""Secret-provider composition without retaining resolved credential values."""

from __future__ import annotations

from collections.abc import Mapping

from stonks_agent.adapters.secrets.factory import create_secret_provider
from stonks_agent.domain.secrets import SecretAccessRequest, SecretRef
from stonks_agent.ports.secret_provider import SecretProvider

LLM_API_KEY_REF = SecretRef(name="llm_api_key")
LLM_API_KEY_ACCESS = SecretAccessRequest(
    reference=LLM_API_KEY_REF,
    purpose="openai_api_key",
)


def build_llm_secret_provider(
    *,
    runtime_environment: str,
    environment: Mapping[str, str],
) -> SecretProvider:
    """Bind one logical LLM credential to its exact local env variable."""

    return create_secret_provider(
        runtime_environment=runtime_environment,
        bindings={LLM_API_KEY_ACCESS: "STONKS_LLM_API_KEY"},
        environment=environment,
    )
