"""Exact cloud-secret lookup through an injected workload-identity client."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.secrets import ResolvedSecret, SecretAccessRequest
from stonks_contracts.common import UTCDateTime

_CLOUD_ENVIRONMENTS = frozenset({"staging", "production"})
_RESOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{1,511}$")


class CloudSecretVersion(BaseModel):
    """Minimal SDK-independent response returned by the workload client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: SecretStr = Field(repr=False, exclude=True)
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}$")
    enabled: bool
    expires_at: UTCDateTime


@runtime_checkable
class WorkloadIdentitySecretClient(Protocol):
    """Cloud SDK boundary authenticated exclusively by the process identity."""

    def access_secret_version(self, resource: str) -> CloudSecretVersion: ...


class CloudSecretProvider:
    """Resolve fresh enabled versions with no stale or local fallback."""

    __slots__ = ("_bindings", "_client", "_clock")

    def __init__(
        self,
        *,
        runtime_environment: str,
        client: WorkloadIdentitySecretClient,
        bindings: Mapping[SecretAccessRequest, str],
        clock: Callable[[], datetime],
    ) -> None:
        if runtime_environment not in _CLOUD_ENVIRONMENTS:
            raise ValueError("cloud secret provider is unavailable")
        if not isinstance(client, WorkloadIdentitySecretClient):
            raise ValueError("cloud secret provider is unavailable")
        self._bindings = _validated_bindings(bindings)
        self._client = client
        self._clock = clock

    def resolve(self, request: SecretAccessRequest) -> Result[ResolvedSecret]:
        resource = self._bindings.get(request)
        if resource is None:
            return _configuration_failure()
        try:
            record: object = self._client.access_secret_version(resource)
            now = self._clock()
            if not isinstance(record, CloudSecretVersion):
                return _unavailable_failure()
            if not record.enabled or record.expires_at <= now:
                return _unavailable_failure()
            resolved = ResolvedSecret(value=record.value, version=record.version)
        except Exception:
            return _unavailable_failure()
        return Success(resolved)

    def __repr__(self) -> str:
        return "CloudSecretProvider(bindings=[REDACTED])"


def _validated_bindings(
    bindings: Mapping[SecretAccessRequest, str],
) -> Mapping[SecretAccessRequest, str]:
    if not bindings:
        raise ValueError("cloud secret provider is unavailable")
    values: dict[SecretAccessRequest, str] = {}
    for request, resource in bindings.items():
        if not isinstance(request, SecretAccessRequest) or not isinstance(
            resource, str
        ):
            raise ValueError("cloud secret provider is unavailable")
        if (
            _RESOURCE.fullmatch(resource) is None
            or "//" in resource
            or ".." in resource.split("/")
        ):
            raise ValueError("cloud secret provider is unavailable")
        values[request] = resource
    return MappingProxyType(values)


def _configuration_failure() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.CONFIGURATION_INVALID,
            message="Secret configuration is invalid",
        )
    )


def _unavailable_failure() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="Secret is unavailable",
        )
    )
