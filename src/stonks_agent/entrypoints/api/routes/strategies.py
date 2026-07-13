"""Strategy registry reads and reviewer-only paper promotion transitions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.adapters.auth.local_token import DenyAllAuthenticator
from stonks_agent.application.strategies.manage import (
    read_strategy,
    read_strategy_events,
    transition_strategy,
)
from stonks_agent.domain.auth import LocalPrincipal
from stonks_agent.domain.errors import ErrorCode, Failure, Result, StructuredError
from stonks_agent.domain.strategy import PromotionState, StrategyTransitionRequest
from stonks_agent.entrypoints.api.envelope import (
    error_envelope,
    success_envelope,
    unexpected_error_envelope,
)
from stonks_agent.entrypoints.api.request_limits import RequestBodyLimitMiddleware
from stonks_agent.entrypoints.api.routes.evaluations import EvaluationEndpoint
from stonks_agent.entrypoints.api.routes.signals import SignalEligibilityEndpoint
from stonks_agent.ports.authentication import AuthenticationRequest, Authenticator
from stonks_agent.ports.strategy_registry import StrategyUnitOfWorkFactory
from stonks_contracts.common import Sha256

MAX_STRATEGY_REQUEST_BYTES = 262_144
type StrategyId = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]
type StrategyVersion = Annotated[str, Path(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]


class TransitionStrategyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_version: int = Field(ge=1)
    current_state: PromotionState
    target_state: PromotionState
    evaluation_report_id: UUID | None = None
    evaluation_hash: Sha256 | None = None
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")


def create_strategy_app(
    unit_of_work: StrategyUnitOfWorkFactory,
    authenticator: Authenticator | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    app = FastAPI(title="Stonks Agent Strategy API", version="0.1.0")
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_STRATEGY_REQUEST_BYTES)
    authenticate = _Authenticator(authenticator or DenyAllAuthenticator())
    selected_clock = clock or _utc_now
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)
    base = "/v1/strategies/{strategy_id}/versions/{strategy_version}"
    app.add_api_route(
        base,
        _StrategyEndpoint(unit_of_work, authenticate),
        methods=["GET"],
    )
    app.add_api_route(
        f"{base}/events",
        _StrategyEventsEndpoint(unit_of_work, authenticate),
        methods=["GET"],
    )
    app.add_api_route(
        f"{base}/transitions",
        _StrategyTransitionEndpoint(unit_of_work, authenticate, selected_clock),
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/evaluations/{report_id}",
        EvaluationEndpoint(unit_of_work, authenticate),
        methods=["GET"],
    )
    app.add_api_route(
        "/v1/signals/eligibility",
        SignalEligibilityEndpoint(unit_of_work, authenticate, selected_clock),
        methods=["POST"],
    )
    return app


class _StrategyEndpoint:
    def __init__(
        self,
        unit_of_work: StrategyUnitOfWorkFactory,
        authenticate: _Authenticator,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._authenticate = authenticate

    def __call__(
        self,
        request: Request,
        strategy_id: StrategyId,
        strategy_version: StrategyVersion,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> JSONResponse:
        principal = self._authenticate(request, authorization)
        if isinstance(principal, Failure):
            return _error_response(principal)
        result = read_strategy(
            principal.value, strategy_id, strategy_version, self._unit_of_work
        )
        return _result_response(result)


class _StrategyEventsEndpoint(_StrategyEndpoint):
    def __call__(
        self,
        request: Request,
        strategy_id: StrategyId,
        strategy_version: StrategyVersion,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> JSONResponse:
        principal = self._authenticate(request, authorization)
        if isinstance(principal, Failure):
            return _error_response(principal)
        result = read_strategy_events(
            principal.value, strategy_id, strategy_version, self._unit_of_work
        )
        return _result_response(result)


class _StrategyTransitionEndpoint:
    def __init__(
        self,
        unit_of_work: StrategyUnitOfWorkFactory,
        authenticate: _Authenticator,
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._authenticate = authenticate
        self._clock = clock

    def __call__(
        self,
        request: Request,
        strategy_id: StrategyId,
        strategy_version: StrategyVersion,
        body: TransitionStrategyBody,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> JSONResponse:
        principal = self._authenticate(request, authorization)
        if isinstance(principal, Failure):
            return _error_response(principal)
        try:
            command = StrategyTransitionRequest(
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                actor=principal.value.subject,
                requested_at=self._clock(),
                **body.model_dump(),
            )
        except ValidationError:
            return _error_response(_failure("Strategy transition is invalid"))
        result = transition_strategy(principal.value, command, self._unit_of_work)
        return _result_response(result)


class _Authenticator:
    def __init__(self, authenticator: Authenticator) -> None:
        self._authenticator = authenticator

    def __call__(
        self, request: Request, authorization: str | None
    ) -> Result[LocalPrincipal]:
        try:
            incoming = AuthenticationRequest(
                authorization=authorization,
                client_host=request.client.host if request.client else None,
            )
        except ValidationError:
            return _failure("Authentication request is invalid")
        return self._authenticator.authenticate(incoming)


def _result_response(result: Result[object]) -> JSONResponse:
    if isinstance(result, Failure):
        return _error_response(result)
    envelope = success_envelope(result.value)
    return JSONResponse(content=envelope.model_dump(mode="json"))


def _error_response(result: Failure) -> JSONResponse:
    envelope = error_envelope(result.error)
    return JSONResponse(
        status_code=envelope.status, content=envelope.model_dump(mode="json")
    )


async def _validation_error(request: Request, error: Exception) -> JSONResponse:
    del request, error
    return _error_response(_failure("Request is invalid"))


async def _unexpected_error(request: Request, error: Exception) -> JSONResponse:
    del request
    envelope = unexpected_error_envelope(error)
    return JSONResponse(
        status_code=envelope.status, content=envelope.model_dump(mode="json")
    )


def _failure(message: str) -> Failure:
    return Failure(StructuredError(code=ErrorCode.INVALID_INPUT, message=message))


def _utc_now() -> datetime:
    return datetime.now(UTC)
