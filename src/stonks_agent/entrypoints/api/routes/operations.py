"""Authenticated paper kill-switch, reconciliation, and audit routes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.adapters.auth.local_token import DenyAllAuthenticator
from stonks_agent.application.operations.activate_kill_switch import (
    activate_kill_switch,
    read_kill_switch,
    read_operator_actions,
)
from stonks_agent.application.operations.reconcile import reconcile_paper_state
from stonks_agent.application.operations.resume import resume_paper
from stonks_agent.domain.errors import ErrorCode, Failure, Result, StructuredError
from stonks_agent.domain.operations import (
    ActivateKillSwitchCommand,
    KillSwitchScope,
    ReconcilePaperCommand,
    ResumePaperCommand,
)
from stonks_agent.entrypoints.api.dependencies.auth import (
    PaperOperatorPrincipal,
    install_authentication,
)
from stonks_agent.entrypoints.api.envelope import (
    error_envelope,
    success_envelope,
    unexpected_error_envelope,
)
from stonks_agent.entrypoints.api.request_limits import RequestBodyLimitMiddleware
from stonks_agent.ports.authentication import Authenticator
from stonks_agent.ports.paper_operations import PaperOperationsUnitOfWorkFactory

MAX_OPERATIONS_REQUEST_BYTES = 65_536


class ActivateKillSwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: UUID
    scope: KillSwitchScope
    account_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_version: int = Field(ge=0)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")


class ReconcilePaperBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: UUID
    account_id: str = Field(min_length=1, max_length=128)


class ResumePaperBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: UUID
    scope: KillSwitchScope
    account_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")


def create_paper_operations_app(
    unit_of_work: PaperOperationsUnitOfWorkFactory,
    authenticator: Authenticator | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    app = FastAPI(title="Stonks Agent Paper Operations API", version="0.1.0")
    app.add_middleware(
        RequestBodyLimitMiddleware, max_bytes=MAX_OPERATIONS_REQUEST_BYTES
    )
    install_authentication(app, authenticator or DenyAllAuthenticator())
    selected_clock = clock or _utc_now
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)
    app.add_api_route(
        "/v1/paper/kill-switches/activate",
        _ActivateEndpoint(unit_of_work, selected_clock),
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/paper/reconciliation",
        _ReconcileEndpoint(unit_of_work, selected_clock),
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/paper/kill-switches/resume",
        _ResumeEndpoint(unit_of_work, selected_clock),
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/paper/kill-switches/{scope}",
        _StatusEndpoint(unit_of_work),
        methods=["GET"],
    )
    app.add_api_route(
        "/v1/paper/operator-actions",
        _ActionsEndpoint(unit_of_work),
        methods=["GET"],
    )
    return app


class _ActivateEndpoint:
    def __init__(
        self,
        unit_of_work: PaperOperationsUnitOfWorkFactory,
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(
        self,
        body: ActivateKillSwitchBody,
        principal: PaperOperatorPrincipal,
    ) -> JSONResponse:
        try:
            command = ActivateKillSwitchCommand(
                **body.model_dump(), requested_at=self._clock()
            )
        except ValidationError:
            return _error_response(_failure("Kill switch request is invalid"))
        return _result_response(
            activate_kill_switch(principal, command, self._unit_of_work)
        )


class _ReconcileEndpoint:
    def __init__(
        self,
        unit_of_work: PaperOperationsUnitOfWorkFactory,
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(
        self,
        body: ReconcilePaperBody,
        principal: PaperOperatorPrincipal,
    ) -> JSONResponse:
        command = ReconcilePaperCommand(**body.model_dump(), requested_at=self._clock())
        return _result_response(
            reconcile_paper_state(principal, command, self._unit_of_work)
        )


class _ResumeEndpoint:
    def __init__(
        self,
        unit_of_work: PaperOperationsUnitOfWorkFactory,
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(
        self,
        body: ResumePaperBody,
        principal: PaperOperatorPrincipal,
    ) -> JSONResponse:
        try:
            command = ResumePaperCommand(
                **body.model_dump(), requested_at=self._clock()
            )
        except ValidationError:
            return _error_response(_failure("Paper resume request is invalid"))
        return _result_response(resume_paper(principal, command, self._unit_of_work))


class _StatusEndpoint:
    def __init__(
        self,
        unit_of_work: PaperOperationsUnitOfWorkFactory,
    ) -> None:
        self._unit_of_work = unit_of_work

    def __call__(
        self,
        scope: KillSwitchScope,
        principal: PaperOperatorPrincipal,
        account_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    ) -> JSONResponse:
        return _result_response(
            read_kill_switch(
                principal,
                scope,
                account_id,
                self._unit_of_work,
            )
        )


class _ActionsEndpoint:
    def __init__(
        self,
        unit_of_work: PaperOperationsUnitOfWorkFactory,
    ) -> None:
        self._unit_of_work = unit_of_work

    def __call__(
        self,
        principal: PaperOperatorPrincipal,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
    ) -> JSONResponse:
        return _result_response(
            read_operator_actions(
                principal,
                after_sequence=after_sequence,
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
