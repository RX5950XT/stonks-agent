"""Exact named-secret lookup for local process environments."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

from pydantic import SecretStr, ValidationError

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.secrets import ResolvedSecret, SecretAccessRequest

_LOCAL_ENVIRONMENTS = frozenset({"local", "development", "test"})
_ENVIRONMENT_VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")


class EnvSecretProvider:
    """Resolve an exact allowlisted request from an injected mutable mapping."""

    __slots__ = ("_bindings", "_environment")

    def __init__(
        self,
        *,
        runtime_environment: str,
        environment: Mapping[str, str],
        bindings: Mapping[SecretAccessRequest, str],
    ) -> None:
        if runtime_environment not in _LOCAL_ENVIRONMENTS:
            raise ValueError("environment secret provider is unavailable")
        self._bindings = _validated_bindings(bindings)
        self._environment = environment

    def resolve(self, request: SecretAccessRequest) -> Result[ResolvedSecret]:
        variable = self._bindings.get(request)
        if variable is None:
            return _configuration_failure()
        raw = self._environment.get(variable)
        if raw is None:
            return _configuration_failure()
        try:
            resolved = ResolvedSecret(
                value=SecretStr(raw),
                version=f"env:{variable}",
            )
        except (TypeError, ValidationError, ValueError):
            return _configuration_failure()
        return Success(resolved)

    def __repr__(self) -> str:
        return "EnvSecretProvider(bindings=[REDACTED])"


def _validated_bindings(
    bindings: Mapping[SecretAccessRequest, str],
) -> Mapping[SecretAccessRequest, str]:
    if not bindings:
        raise ValueError("environment secret provider is unavailable")
    values: dict[SecretAccessRequest, str] = {}
    for request, variable in bindings.items():
        if not isinstance(request, SecretAccessRequest) or not isinstance(
            variable, str
        ):
            raise ValueError("environment secret provider is unavailable")
        if _ENVIRONMENT_VARIABLE.fullmatch(variable) is None:
            raise ValueError("environment secret provider is unavailable")
        values[request] = variable
    return MappingProxyType(values)


def _configuration_failure() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.CONFIGURATION_INVALID,
            message="Secret configuration is invalid",
        )
    )
