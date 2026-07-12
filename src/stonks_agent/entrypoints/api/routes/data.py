"""Reference-only data snapshot ingestion API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.adapters.auth.local_token import DenyAllAuthenticator
from stonks_agent.application.data.create_snapshot import request_snapshot
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.entrypoints.api.envelope import (
    error_envelope,
    success_envelope,
    unexpected_error_envelope,
)
from stonks_agent.entrypoints.api.request_limits import RequestBodyLimitMiddleware
from stonks_agent.ports.authentication import AuthenticationRequest, Authenticator
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
) -> FastAPI:
    app = FastAPI(title="Stonks Agent Data API", version="0.1.0")
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=MAX_SNAPSHOT_REQUEST_BYTES,
    )
    identity = authenticator or DenyAllAuthenticator()
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)
    app.add_api_route(
        "/v1/data/snapshots",
        _CreateSnapshotEndpoint(store, identity, clock or _utc_now),
        methods=["POST"],
        status_code=202,
    )
    return app


async def _validation_error(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request, error
    return _error_response(
        Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Request body is invalid",
            )
        )
    )


async def _unexpected_error(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del request
    envelope = unexpected_error_envelope(error)
    return JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
    )


class _CreateSnapshotEndpoint:
    def __init__(
        self,
        store: SnapshotRequestStore,
        identity: Authenticator,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._identity = identity
        self._clock = clock

    def __call__(
        self,
        request_context: Request,
        body: CreateSnapshotBody,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> JSONResponse:
        client = request_context.client
        authentication = _authentication_request(
            authorization,
            client.host if client is not None else None,
        )
        if isinstance(authentication, Failure):
            return _error_response(authentication)
        principal = self._identity.authenticate(authentication)
        if isinstance(principal, Failure):
            return _error_response(principal)
        try:
            request = CreateSnapshotRequest(
                **body.model_dump(),
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
        result = request_snapshot(principal.value, request, self._store)
        if isinstance(result, Failure):
            return _error_response(result)
        envelope = success_envelope(result.value, status=202)
        return JSONResponse(
            status_code=202,
            content=envelope.model_dump(mode="json"),
        )


def _authentication_request(
    authorization: str | None,
    client_host: str | None,
) -> AuthenticationRequest | Failure:
    try:
        return AuthenticationRequest(
            authorization=authorization,
            client_host=client_host,
        )
    except ValidationError:
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Authentication request is invalid",
            )
        )


def _error_response(result: Failure) -> JSONResponse:
    envelope = error_envelope(result.error)
    return JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
