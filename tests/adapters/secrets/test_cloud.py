from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from stonks_agent.adapters.secrets.cloud import (
    CloudSecretProvider,
    CloudSecretVersion,
    WorkloadIdentitySecretClient,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.secrets import SecretAccessRequest, SecretRef

NOW = datetime(2026, 7, 16, tzinfo=UTC)
REQUEST = SecretAccessRequest(
    reference=SecretRef(name="openai_api_key"),
    purpose="llm.openai",
)
RESOURCE = "projects/stonks-prod/secrets/openai-api-key/versions/latest"


class RecordingClient:
    def __init__(self, version: CloudSecretVersion) -> None:
        self.version = version
        self.resources: list[str] = []

    def access_secret_version(self, resource: str) -> CloudSecretVersion:
        self.resources.append(resource)
        return self.version


def version(
    value: str = "first-cloud-sensitive-value",
    *,
    name: str = "42",
    enabled: bool = True,
    expires_at: datetime = NOW + timedelta(minutes=5),
) -> CloudSecretVersion:
    return CloudSecretVersion(
        value=SecretStr(value),
        version=name,
        enabled=enabled,
        expires_at=expires_at,
    )


def test_cloud_client_and_provider_are_runtime_checkable_and_rotate() -> None:
    client = RecordingClient(version())
    provider = CloudSecretProvider(
        runtime_environment="production",
        client=client,
        bindings={REQUEST: RESOURCE},
        clock=lambda: NOW,
    )

    first = provider.resolve(REQUEST)
    client.version = version("rotated-cloud-sensitive-value", name="43")
    second = provider.resolve(REQUEST)

    assert isinstance(client, WorkloadIdentitySecretClient)
    assert isinstance(first, Success)
    assert first.value.reveal() == "first-cloud-sensitive-value"
    assert first.value.version == "42"
    assert isinstance(second, Success)
    assert second.value.reveal() == "rotated-cloud-sensitive-value"
    assert second.value.version == "43"
    assert client.resources == [RESOURCE, RESOURCE]
    assert "rotated-cloud-sensitive-value" not in repr(client.version)
    assert "rotated-cloud-sensitive-value" not in repr(provider)


def test_cloud_provider_denies_unknown_binding_before_client_call() -> None:
    client = RecordingClient(version())
    provider = CloudSecretProvider(
        runtime_environment="staging",
        client=client,
        bindings={REQUEST: RESOURCE},
        clock=lambda: NOW,
    )

    result = provider.resolve(REQUEST.model_copy(update={"purpose": "llm.other"}))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFIGURATION_INVALID
    assert client.resources == []


@pytest.mark.parametrize(
    "record",
    [
        version(enabled=False),
        version(expires_at=NOW),
        version(expires_at=NOW - timedelta(seconds=1)),
    ],
)
def test_cloud_provider_denies_disabled_or_stale_versions_without_fallback(
    record: CloudSecretVersion,
) -> None:
    client = RecordingClient(record)
    provider = CloudSecretProvider(
        runtime_environment="production",
        client=client,
        bindings={REQUEST: RESOURCE},
        clock=lambda: NOW,
    )

    result = provider.resolve(REQUEST)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert record.value.get_secret_value() not in repr(result.error)


def test_cloud_provider_maps_outage_to_public_safe_failure_without_fallback() -> None:
    class OutageClient:
        def access_secret_version(self, resource: str) -> CloudSecretVersion:
            raise RuntimeError(f"backend leaked {resource} sensitive-value")

    provider = CloudSecretProvider(
        runtime_environment="production",
        client=OutageClient(),
        bindings={REQUEST: RESOURCE},
        clock=lambda: NOW,
    )

    result = provider.resolve(REQUEST)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert "sensitive-value" not in repr(result.error)
    assert RESOURCE not in repr(result.error)


@pytest.mark.parametrize(
    "secret",
    ["", "   ", " leading", "trailing ", "line\nbreak", "unicode\u0085control"],
)
def test_cloud_provider_denies_invalid_secret_without_disclosure(secret: str) -> None:
    client = RecordingClient(version(secret))
    provider = CloudSecretProvider(
        runtime_environment="production",
        client=client,
        bindings={REQUEST: RESOURCE},
        clock=lambda: NOW,
    )

    result = provider.resolve(REQUEST)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert secret not in repr(result.error) or not secret
    assert secret not in repr(provider) or not secret
