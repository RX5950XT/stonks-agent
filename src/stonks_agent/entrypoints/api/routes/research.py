"""Queue-only research commands and canonical run-event SSE projection."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.adapters.auth.local_token import DenyAllAuthenticator
from stonks_agent.application.research.request_run import (
    read_run_events,
    request_research_run,
)
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.redaction import redact
from stonks_agent.domain.research_run import CanonicalRunEvent, ResearchRunRequest
from stonks_agent.entrypoints.api.dependencies.auth import (
    ReadPrincipal,
    ResearchPrincipal,
    install_authentication,
)
from stonks_agent.entrypoints.api.envelope import (
    error_envelope,
    success_envelope,
    unexpected_error_envelope,
)
from stonks_agent.entrypoints.api.request_limits import RequestBodyLimitMiddleware
from stonks_agent.entrypoints.api.routes.reports import ReportEndpoint
from stonks_agent.ports.authentication import Authenticator
from stonks_agent.ports.research_query import (
    ReportReader,
    ResearchRequestStore,
    RunEventReader,
)
from stonks_contracts.common import UTCDateTime

MAX_RESEARCH_REQUEST_BYTES = 32_768


class CreateResearchRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    as_of: UTCDateTime
    snapshot_id: UUID
    research_profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
    model_policy_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
    language: str = Field(pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")


def create_research_app(
    requests: ResearchRequestStore,
    events: RunEventReader,
    reports: ReportReader,
    authenticator: Authenticator | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    app = FastAPI(title="Stonks Agent Research API", version="0.1.0")
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_RESEARCH_REQUEST_BYTES)
    identity = authenticator or DenyAllAuthenticator()
    install_authentication(app, identity)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)
    app.add_api_route(
        "/v1/research/runs",
        _CreateResearchEndpoint(requests, clock or _utc_now),
        methods=["POST"],
        status_code=202,
    )
    app.add_api_route(
        "/v1/research/runs/{run_id}/events",
        _RunEventsEndpoint(events),
        methods=["GET"],
        response_model=None,
    )
    app.add_api_route(
        "/v1/reports/{content_hash}",
        ReportEndpoint(reports),
        methods=["GET"],
    )
    return app


class _CreateResearchEndpoint:
    def __init__(
        self,
        store: ResearchRequestStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._store, self._clock = store, clock

    def __call__(
        self,
        body: CreateResearchRunBody,
        principal: ResearchPrincipal,
    ) -> JSONResponse:
        try:
            command = ResearchRunRequest(
                **body.model_dump(),
                owner_subject=principal.subject,
                requested_at=self._clock(),
            )
        except ValidationError:
            return _error_response(
                _failure(ErrorCode.INVALID_INPUT, "Research request is invalid")
            )
        result = request_research_run(principal, command, self._store)
        if isinstance(result, Failure):
            return _error_response(result)
        envelope = success_envelope(result.value, status=202)
        return JSONResponse(status_code=202, content=envelope.model_dump(mode="json"))


class _RunEventsEndpoint:
    def __init__(self, reader: RunEventReader) -> None:
        self._reader = reader

    def __call__(
        self,
        run_id: UUID,
        principal: ReadPrincipal,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> JSONResponse | StreamingResponse:
        cursor = _cursor(last_event_id)
        if isinstance(cursor, Failure):
            return _error_response(cursor)
        result = read_run_events(
            principal,
            run_id,
            after_sequence=cursor.value,
            limit=limit,
            reader=self._reader,
        )
        if isinstance(result, Failure):
            return _error_response(result)
        return StreamingResponse(
            _sse(result.value),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )


def _cursor(value: str | None) -> Success[int] | Failure:
    if value is None:
        return Success(0)
    try:
        parsed = int(value)
    except ValueError:
        return _failure(ErrorCode.INVALID_INPUT, "Last-Event-ID is invalid")
    if parsed < 0:
        return _failure(ErrorCode.INVALID_INPUT, "Last-Event-ID is invalid")
    return Success(parsed)


def _sse(events: tuple[CanonicalRunEvent, ...]) -> Iterator[str]:
    for event in events:
        projection = event.model_dump(mode="json")
        projection["payload"] = cast(dict[str, object], redact(event.payload))
        envelope = success_envelope(projection)
        data = json.dumps(
            envelope.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"


async def _validation_error(request: Request, error: Exception) -> JSONResponse:
    del request, error
    return _error_response(_failure(ErrorCode.INVALID_INPUT, "Request is invalid"))


async def _unexpected_error(request: Request, error: Exception) -> JSONResponse:
    del request
    envelope = unexpected_error_envelope(error)
    return JSONResponse(
        status_code=envelope.status, content=envelope.model_dump(mode="json")
    )


def _error_response(result: Failure) -> JSONResponse:
    envelope = error_envelope(result.error)
    return JSONResponse(
        status_code=envelope.status, content=envelope.model_dump(mode="json")
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))


def _utc_now() -> datetime:
    return datetime.now(UTC)
