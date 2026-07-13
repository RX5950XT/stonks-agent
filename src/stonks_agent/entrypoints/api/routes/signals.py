"""Read-only signal eligibility endpoint with exact provenance resolution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Protocol

from fastapi import Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from stonks_agent.application.strategies.manage import check_signal_eligibility
from stonks_agent.domain.auth import LocalPrincipal
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.domain.signal import AlphaSignal
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope
from stonks_agent.ports.strategy_registry import StrategyUnitOfWorkFactory


class SignalEligibilityBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal: AlphaSignal


class Authenticate(Protocol):
    def __call__(
        self, request: Request, authorization: str | None
    ) -> Result[LocalPrincipal]: ...


class SignalEligibilityEndpoint:
    def __init__(
        self,
        unit_of_work: StrategyUnitOfWorkFactory,
        authenticate: Authenticate,
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._authenticate = authenticate
        self._clock = clock

    def __call__(
        self,
        request: Request,
        body: SignalEligibilityBody,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> JSONResponse:
        principal = self._authenticate(request, authorization)
        if isinstance(principal, Failure):
            return _error_response(principal)
        result = check_signal_eligibility(
            principal.value,
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
