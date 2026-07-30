"""Reference-only data snapshot ingestion API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.adapters.auth.local_token import DenyAllAuthenticator
from stonks_agent.application.data.create_snapshot import request_snapshot
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.entrypoints.api.api_security import (
    ApiSecurityOptions,
    install_api_security,
)
from stonks_agent.entrypoints.api.dependencies.auth import (
    ResearchPrincipal,
    install_authentication,
)
from stonks_agent.entrypoints.api.envelope import (
    error_envelope,
    success_envelope,
)
from stonks_agent.entrypoints.api.telemetry import (
    ApiTelemetryOptions,
    install_api_telemetry,
)
from stonks_agent.ports.authentication import Authenticator
from stonks_agent.ports.snapshot_request import SnapshotRequestStore
from stonks_contracts.common import UTCDateTime

MAX_SNAPSHOT_REQUEST_BYTES = 65_536


class CreateSnapshotBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    capability: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    as_of: UTCDateTime
    query: dict[str, object]
    provider_policy_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)


def create_data_app(
    store: SnapshotRequestStore,
    authenticator: Authenticator | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    api_security: ApiSecurityOptions | None = None,
    api_telemetry: ApiTelemetryOptions | None = None,
) -> FastAPI:
    app = FastAPI(title="Stonks Agent Data API", version="0.1.0")
    install_api_security(
        app,
        max_request_bytes=MAX_SNAPSHOT_REQUEST_BYTES,
        options=api_security,
    )
    install_api_telemetry(app, options=api_telemetry)
    identity = authenticator or DenyAllAuthenticator()
    install_authentication(app, identity)
    app.add_api_route(
        "/v1/data/snapshots",
        _CreateSnapshotEndpoint(store, clock or utc_now),
        methods=["POST"],
        status_code=202,
    )
    return app


class _CreateSnapshotEndpoint:
    def __init__(
        self,
        store: SnapshotRequestStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._clock = clock

    def __call__(
        self,
        body: CreateSnapshotBody,
        principal: ResearchPrincipal,
    ) -> JSONResponse:
        try:
            request = CreateSnapshotRequest(
                **body.model_dump(),
                owner_subject=principal.subject,
                requested_at=self._clock(),
            )
        except ValidationError:
            return _error_response(
                Failure(
                    StructuredError(
                        code=ErrorCode.INVALID_INPUT,
                        message="Snapshot request is invalid",
                    )
                )
            )
        result = request_snapshot(principal, request, self._store)
        if isinstance(result, Failure):
            return _error_response(result)
        envelope = success_envelope(result.value, status=202)
        return JSONResponse(
            status_code=202,
            content=envelope.model_dump(mode="json"),
        )


def _error_response(result: Failure) -> JSONResponse:
    envelope = error_envelope(result.error)
    return JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
    )
