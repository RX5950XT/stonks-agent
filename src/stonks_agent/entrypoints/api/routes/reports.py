"""Read-only report endpoint implementation."""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import Header, Request
from fastapi.responses import JSONResponse

from stonks_agent.application.research.request_run import read_report
from stonks_agent.domain.auth import LocalPrincipal
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.entrypoints.api.envelope import success_envelope
from stonks_agent.ports.research_query import ReportReader


class Authenticate(Protocol):
    def __call__(
        self, request: Request, authorization: str | None
    ) -> Result[LocalPrincipal]: ...


class ReportEndpoint:
    def __init__(self, reader: ReportReader, authenticate: Authenticate) -> None:
        self._reader = reader
        self._authenticate = authenticate

    def __call__(
        self,
        request: Request,
        content_hash: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> JSONResponse:
        principal = self._authenticate(request, authorization)
        if isinstance(principal, Failure):
            return _error_response(principal)
        result = read_report(principal.value, content_hash, self._reader)
        if isinstance(result, Failure):
            return _error_response(result)
        envelope = success_envelope(result.value)
        return JSONResponse(content=envelope.model_dump(mode="json"))


def _error_response(result: Failure) -> JSONResponse:
    from stonks_agent.entrypoints.api.envelope import error_envelope

    envelope = error_envelope(result.error)
    return JSONResponse(
        status_code=envelope.status, content=envelope.model_dump(mode="json")
    )
