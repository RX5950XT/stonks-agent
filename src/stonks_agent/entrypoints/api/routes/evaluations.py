"""Read-only immutable evaluation report endpoint."""

from __future__ import annotations

from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Header, Request
from fastapi.responses import JSONResponse

from stonks_agent.application.strategies.manage import read_evaluation
from stonks_agent.domain.auth import LocalPrincipal
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope
from stonks_agent.ports.strategy_registry import StrategyUnitOfWorkFactory


class Authenticate(Protocol):
    def __call__(
        self, request: Request, authorization: str | None
    ) -> Result[LocalPrincipal]: ...


class EvaluationEndpoint:
    def __init__(
        self,
        unit_of_work: StrategyUnitOfWorkFactory,
        authenticate: Authenticate,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._authenticate = authenticate

    def __call__(
        self,
        request: Request,
        report_id: UUID,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> JSONResponse:
        principal = self._authenticate(request, authorization)
        if isinstance(principal, Failure):
            return _error_response(principal)
        result = read_evaluation(principal.value, report_id, self._unit_of_work)
        if isinstance(result, Failure):
            return _error_response(result)
        payload = result.value.model_dump(mode="json") | {
            "evaluation_hash": result.value.evaluation_hash
        }
        envelope = success_envelope(payload)
        return JSONResponse(content=envelope.model_dump(mode="json"))


def _error_response(result: Failure) -> JSONResponse:
    envelope = error_envelope(result.error)
    return JSONResponse(
        status_code=envelope.status, content=envelope.model_dump(mode="json")
    )
