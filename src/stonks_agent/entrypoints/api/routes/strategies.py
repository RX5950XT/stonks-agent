"""Strategy registry reads and reviewer-only paper promotion transitions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.adapters.auth.local_token import DenyAllAuthenticator
from stonks_agent.application.strategies.manage import (
    read_strategy,
    read_strategy_events,
    transition_strategy,
)
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.errors import ErrorCode, Failure, Result, StructuredError
from stonks_agent.domain.strategy import PromotionState, StrategyTransitionRequest
from stonks_agent.entrypoints.api.api_security import (
    ApiSecurityOptions,
    install_api_security,
)
from stonks_agent.entrypoints.api.dependencies.auth import (
    ReadPrincipal,
    StrategyReviewerPrincipal,
    install_authentication,
)
from stonks_agent.entrypoints.api.envelope import (
    error_envelope,
    success_envelope,
)
from stonks_agent.entrypoints.api.routes.evaluations import EvaluationEndpoint
from stonks_agent.entrypoints.api.routes.signals import SignalEligibilityEndpoint
from stonks_agent.entrypoints.api.telemetry import (
    ApiTelemetryOptions,
    install_api_telemetry,
)
from stonks_agent.ports.authentication import Authenticator
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
    api_security: ApiSecurityOptions | None = None,
    api_telemetry: ApiTelemetryOptions | None = None,
) -> FastAPI:
    app = FastAPI(title="Stonks Agent Strategy API", version="0.1.0")
    install_api_security(
        app,
        max_request_bytes=MAX_STRATEGY_REQUEST_BYTES,
        options=api_security,
    )
    install_api_telemetry(app, options=api_telemetry)
    install_authentication(app, authenticator or DenyAllAuthenticator())
    selected_clock = clock or utc_now
    base = "/v1/strategies/{strategy_id}/versions/{strategy_version}"
    app.add_api_route(
        base,
        _StrategyEndpoint(unit_of_work),
        methods=["GET"],
    )
    app.add_api_route(
        f"{base}/events",
        _StrategyEventsEndpoint(unit_of_work),
        methods=["GET"],
    )
    app.add_api_route(
        f"{base}/transitions",
        _StrategyTransitionEndpoint(unit_of_work, selected_clock),
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/evaluations/{report_id}",
        EvaluationEndpoint(unit_of_work),
        methods=["GET"],
    )
    app.add_api_route(
        "/v1/signals/eligibility",
        SignalEligibilityEndpoint(unit_of_work, selected_clock),
        methods=["POST"],
    )
    return app


class _StrategyEndpoint:
    def __init__(
        self,
        unit_of_work: StrategyUnitOfWorkFactory,
    ) -> None:
        self._unit_of_work = unit_of_work

    def __call__(
        self,
        strategy_id: StrategyId,
        strategy_version: StrategyVersion,
        principal: ReadPrincipal,
    ) -> JSONResponse:
        result = read_strategy(
            principal, strategy_id, strategy_version, self._unit_of_work
        )
        return _result_response(result)


class _StrategyEventsEndpoint(_StrategyEndpoint):
    def __call__(
        self,
        strategy_id: StrategyId,
        strategy_version: StrategyVersion,
        principal: ReadPrincipal,
    ) -> JSONResponse:
        result = read_strategy_events(
            principal, strategy_id, strategy_version, self._unit_of_work
        )
        return _result_response(result)


class _StrategyTransitionEndpoint:
    def __init__(
        self,
        unit_of_work: StrategyUnitOfWorkFactory,
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(
        self,
        strategy_id: StrategyId,
        strategy_version: StrategyVersion,
        body: TransitionStrategyBody,
        principal: StrategyReviewerPrincipal,
    ) -> JSONResponse:
        try:
            command = StrategyTransitionRequest(
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                actor=principal.subject,
                requested_at=self._clock(),
                **body.model_dump(),
            )
        except ValidationError:
            return _error_response(_failure("Strategy transition is invalid"))
        result = transition_strategy(principal, command, self._unit_of_work)
        return _result_response(result)


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


def _failure(message: str) -> Failure:
    return Failure(StructuredError(code=ErrorCode.INVALID_INPUT, message=message))
