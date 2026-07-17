"""Fail-closed runtime selection for secret providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from stonks_agent.adapters.secrets.cloud import (
    CloudSecretProvider,
    WorkloadIdentitySecretClient,
)
from stonks_agent.adapters.secrets.env import EnvSecretProvider
from stonks_agent.domain.secrets import SecretAccessRequest
from stonks_agent.ports.secret_provider import SecretProvider

_LOCAL_ENVIRONMENTS = frozenset({"local", "development", "test"})
_CLOUD_ENVIRONMENTS = frozenset({"staging", "production"})


def create_secret_provider(
    *,
    runtime_environment: str,
    bindings: Mapping[SecretAccessRequest, str],
    environment: Mapping[str, str] | None = None,
    cloud_client: WorkloadIdentitySecretClient | None = None,
    clock: Callable[[], datetime] | None = None,
) -> SecretProvider:
    try:
        if runtime_environment in _LOCAL_ENVIRONMENTS:
            if environment is None or cloud_client is not None:
                raise ValueError
            return EnvSecretProvider(
                runtime_environment=runtime_environment,
                environment=environment,
                bindings=bindings,
            )
        if runtime_environment in _CLOUD_ENVIRONMENTS:
            if cloud_client is None or environment is not None:
                raise ValueError
            return CloudSecretProvider(
                runtime_environment=runtime_environment,
                client=cloud_client,
                bindings=bindings,
                clock=clock or (lambda: datetime.now(UTC)),
            )
    except (TypeError, ValueError) as error:
        raise ValueError("secret provider configuration is invalid") from error
    raise ValueError("secret provider configuration is invalid")
