from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from stonks_agent.adapters.secrets.cloud import CloudSecretVersion
from stonks_agent.adapters.secrets.env import EnvSecretProvider
from stonks_agent.adapters.secrets.factory import create_secret_provider
from stonks_agent.domain.secrets import SecretAccessRequest, SecretRef

REQUEST = SecretAccessRequest(
    reference=SecretRef(name="openai_api_key"),
    purpose="llm.openai",
)


class Client:
    def access_secret_version(self, resource: str) -> CloudSecretVersion:
        return CloudSecretVersion(
            value=SecretStr("cloud-sensitive-value"),
            version="1",
            enabled=True,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_factory_selects_env_only_for_explicit_local_environments() -> None:
    provider = create_secret_provider(
        runtime_environment="test",
        bindings={REQUEST: "OPENAI_API_KEY"},
        environment={"OPENAI_API_KEY": "local-sensitive-value"},
    )

    assert isinstance(provider, EnvSecretProvider)


def test_factory_selects_cloud_only_for_staging_or_production() -> None:
    provider = create_secret_provider(
        runtime_environment="production",
        bindings={REQUEST: "projects/stonks/secrets/openai-api-key/versions/latest"},
        cloud_client=Client(),
    )

    assert provider.__class__.__name__ == "CloudSecretProvider"


@pytest.mark.parametrize("runtime_environment", ["", "prod", "ci", "unknown"])
def test_factory_rejects_unknown_runtime_environment(
    runtime_environment: str,
) -> None:
    with pytest.raises(ValueError, match="secret provider configuration is invalid"):
        create_secret_provider(
            runtime_environment=runtime_environment,
            bindings={REQUEST: "OPENAI_API_KEY"},
            environment={"OPENAI_API_KEY": "sensitive-value"},
            cloud_client=Client(),
        )


def test_factory_does_not_fallback_between_env_and_cloud() -> None:
    with pytest.raises(ValueError, match="secret provider configuration is invalid"):
        create_secret_provider(
            runtime_environment="production",
            bindings={REQUEST: "OPENAI_API_KEY"},
            environment={"OPENAI_API_KEY": "sensitive-value"},
        )
    with pytest.raises(ValueError, match="secret provider configuration is invalid"):
        create_secret_provider(
            runtime_environment="local",
            bindings={REQUEST: "OPENAI_API_KEY"},
            cloud_client=Client(),
        )
