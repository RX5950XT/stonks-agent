"""Read-only signal eligibility endpoint with exact provenance resolution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from stonks_agent.application.strategies.manage import check_signal_eligibility
from stonks_agent.domain.errors import Failure
from stonks_agent.domain.signal import AlphaSignal
from stonks_agent.entrypoints.api.dependencies.auth import ReadPrincipal
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope
from stonks_agent.ports.strategy_registry import StrategyUnitOfWorkFactory


class SignalEligibilityBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal: AlphaSignal


class SignalEligibilityEndpoint:
    def __init__(
        self,
        unit_of_work: StrategyUnitOfWorkFactory,
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(
        self,
        body: SignalEligibilityBody,
        principal: ReadPrincipal,
    ) -> JSONResponse:
        result = check_signal_eligibility(
            principal,
            body.signal,
            at=self._clock(),
            unit_of_work=self._unit_of_work,
        )
        if isinstance(result, Failure):
            return _error_response(result)
        envelope = success_envelope(result.value)
        return JSONResponse(content=envelope.model_dump(mode="json"))


def _error_response(result: Failure) -> JSONResponse:
    envelope = error_envelope(result.error)
    return JSONResponse(
        status_code=envelope.status, content=envelope.model_dump(mode="json")
    )
