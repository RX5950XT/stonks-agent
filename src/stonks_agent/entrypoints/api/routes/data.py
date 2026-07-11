"""Reference-only data snapshot ingestion API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.application.data.create_snapshot import request_snapshot
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope
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


def create_data_app(store: SnapshotRequestStore) -> FastAPI:
    app = FastAPI(title="Stonks Agent Data API", version="0.1.0")

    @app.post("/v1/data/snapshots", status_code=202)
    def create_snapshot(
        body: CreateSnapshotBody,
        subject: Annotated[
            str | None,
            Header(alias="X-Local-Subject"),
        ] = None,
        roles: Annotated[
            str | None,
            Header(alias="X-Local-Roles"),
        ] = None,
    ) -> JSONResponse:
        principal = _local_principal(subject, roles)
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
        result = request_snapshot(principal, request, store)
        if isinstance(result, Failure):
            return _error_response(result)
        envelope = success_envelope(result.value, status=202)
        return JSONResponse(
            status_code=202,
            content=envelope.model_dump(mode="json"),
        )

    return app


def _local_principal(subject: str | None, roles: str | None) -> LocalPrincipal | Failure:
    if not subject or not roles:
        return Failure(
            StructuredError(
                code=ErrorCode.UNAUTHORIZED,
                message="Local identity is required",
            )
        )
    try:
        parsed_roles = frozenset(Role(item.strip()) for item in roles.split(","))
        return LocalPrincipal(subject=subject, roles=parsed_roles)
    except (ValueError, ValidationError):
        return Failure(
            StructuredError(
                code=ErrorCode.UNAUTHORIZED,
                message="Local identity is invalid",
            )
        )


def _error_response(result: Failure) -> JSONResponse:
    envelope = error_envelope(result.error)
    return JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
    )
