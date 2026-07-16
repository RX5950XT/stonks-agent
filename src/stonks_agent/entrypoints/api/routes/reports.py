"""Read-only report endpoint implementation."""

from __future__ import annotations

from typing import Annotated

from fastapi import Path
from fastapi.responses import JSONResponse

from stonks_agent.application.research.request_run import read_report
from stonks_agent.domain.errors import Failure
from stonks_agent.entrypoints.api.dependencies.auth import ReadPrincipal
from stonks_agent.entrypoints.api.envelope import success_envelope
from stonks_agent.ports.research_query import ReportReader


class ReportEndpoint:
    def __init__(self, reader: ReportReader) -> None:
        self._reader = reader

    def __call__(
        self,
        content_hash: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
        principal: ReadPrincipal,
    ) -> JSONResponse:
        result = read_report(principal, content_hash, self._reader)
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
