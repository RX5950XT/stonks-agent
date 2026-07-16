"""Read-only immutable evaluation report endpoint."""

from __future__ import annotations

from uuid import UUID

from fastapi.responses import JSONResponse

from stonks_agent.application.strategies.manage import read_evaluation
from stonks_agent.domain.errors import Failure
from stonks_agent.entrypoints.api.dependencies.auth import ReadPrincipal
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope
from stonks_agent.ports.strategy_registry import StrategyUnitOfWorkFactory


class EvaluationEndpoint:
    def __init__(
        self,
        unit_of_work: StrategyUnitOfWorkFactory,
    ) -> None:
        self._unit_of_work = unit_of_work

    def __call__(
        self,
        report_id: UUID,
        principal: ReadPrincipal,
    ) -> JSONResponse:
        result = read_evaluation(principal, report_id, self._unit_of_work)
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
