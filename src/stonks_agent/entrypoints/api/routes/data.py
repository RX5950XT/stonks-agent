"""Reference-only data snapshot ingestion API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.adapters.auth.local_token import DenyAllAuthenticator
from stonks_agent.application.data.create_snapshot import request_snapshot
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope
from stonks_agent.ports.authentication import AuthenticationRequest, Authenticator
from stonks_agent.ports.snapshot_request import SnapshotRequestStore
from stonks_contracts.common import UTCDateTime


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
) -> FastAPI:
    app = FastAPI(title="Stonks Agent Data API", version="0.1.0")
    identity = authenticator or DenyAllAuthenticator()

    @app.post("/v1/data/snapshots", status_code=202)
    def create_snapshot(
        request_context: Request,
        body: CreateSnapshotBody,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> JSONResponse:
        client = request_context.client
        principal = identity.authenticate(
            AuthenticationRequest(
                authorization=authorization,
                client_host=client.host if client is not None else None,
            )
        )
        if isinstance(principal, Failure):
            return _error_response(principal)
        try:
            request = CreateSnapshotRequest(
                **body.model_dump(),
                requested_at=datetime.now(UTC),
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
        result = request_snapshot(principal.value, request, store)
        if isinstance(result, Failure):
            return _error_response(result)
        envelope = success_envelope(result.value, status=202)
        return JSONResponse(
            status_code=202,
            content=envelope.model_dump(mode="json"),
        )

    return app


def _error_response(result: Failure) -> JSONResponse:
    envelope = error_envelope(result.error)
    return JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
    )
