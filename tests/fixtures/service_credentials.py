"""Recording short-lived credential provider for core HTTP adapter tests."""

from __future__ import annotations

from pydantic import SecretStr

from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.ports.service_credentials import (
    ServiceBearerCredential,
    ServiceCredentialRequest,
)

TEST_SERVICE_TOKEN = "test-core-runner-credential-32-bytes"


class RecordingServiceCredentialProvider:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.requests: list[ServiceCredentialRequest] = []

    def issue(
        self,
        request: ServiceCredentialRequest,
    ) -> Success[ServiceBearerCredential] | Failure:
        self.requests.append(request)
        if not self.available:
            return Failure(
                StructuredError(
                    code=ErrorCode.UNAUTHORIZED,
                    message="Service credential unavailable",
                )
            )
        return Success(ServiceBearerCredential(token=SecretStr(TEST_SERVICE_TOKEN)))
