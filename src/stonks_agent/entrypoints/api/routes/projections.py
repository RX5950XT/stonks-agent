"""Authenticated read-only paper portfolio, NAV, and risk routes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import FastAPI, Path
from fastapi.responses import JSONResponse

from stonks_agent.adapters.auth.local_token import DenyAllAuthenticator
from stonks_agent.application.projections.queries import (
    read_nav_projection,
    read_portfolio_projection,
    read_risk_projection,
)
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.entrypoints.api.api_security import (
    ApiSecurityOptions,
    install_api_security,
)
from stonks_agent.entrypoints.api.dependencies.auth import (
    ReadPrincipal,
    install_authentication,
)
from stonks_agent.entrypoints.api.envelope import (
    error_envelope,
    success_envelope,
)
from stonks_agent.entrypoints.api.telemetry import (
    ApiTelemetryOptions,
    install_api_telemetry,
)
from stonks_agent.ports.authentication import Authenticator
from stonks_agent.ports.paper_projections import PaperProjectionUnitOfWorkFactory

type AccountId = Annotated[
    str,
    Path(pattern=r"^[A-Za-z0-9_.:-]{1,128}$"),
]
MAX_PROJECTION_REQUEST_BYTES = 16_384


def create_paper_projection_app(
    unit_of_work: PaperProjectionUnitOfWorkFactory,
    authenticator: Authenticator | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    api_security: ApiSecurityOptions | None = None,
    api_telemetry: ApiTelemetryOptions | None = None,
) -> FastAPI:
    app = FastAPI(title="Stonks Agent Paper Projection API", version="0.1.0")
    install_api_security(
        app,
        max_request_bytes=MAX_PROJECTION_REQUEST_BYTES,
        options=api_security,
    )
    install_api_telemetry(app, options=api_telemetry)
    install_authentication(app, authenticator or DenyAllAuthenticator())
    selected_clock = clock or utc_now
    base = "/v1/paper/accounts/{account_id}"
    for view in ("portfolio", "nav", "risk"):
        app.add_api_route(
            f"{base}/{view}",
            _ProjectionEndpoint(unit_of_work, selected_clock, view),
            methods=["GET"],
        )
    return app


class _ProjectionEndpoint:
    def __init__(
        self,
        unit_of_work: PaperProjectionUnitOfWorkFactory,
        clock: Callable[[], datetime],
        view: Literal["portfolio", "nav", "risk"],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._view = view

    def __call__(
        self,
        account_id: AccountId,
        principal: ReadPrincipal,
    ) -> JSONResponse:
        if self._view == "portfolio":
            return _result_response(
                read_portfolio_projection(principal, account_id, self._unit_of_work)
            )
        if self._view == "nav":
            return _result_response(
                read_nav_projection(principal, account_id, self._unit_of_work)
            )
        return _result_response(
            read_risk_projection(
                principal,
                account_id,
                as_of=self._clock(),
                unit_of_work=self._unit_of_work,
            )
        )


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
