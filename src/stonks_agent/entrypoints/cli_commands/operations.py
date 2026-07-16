"""Local audited paper operator commands."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

import typer
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine

from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.application.operations.activate_kill_switch import (
    activate_kill_switch,
    read_kill_switch,
    read_operator_actions,
)
from stonks_agent.application.operations.reconcile import reconcile_paper_state
from stonks_agent.application.operations.resume import resume_paper
from stonks_agent.application.projections.queries import (
    read_nav_projection,
    read_portfolio_projection,
    read_risk_projection,
)
from stonks_agent.domain.auth import AccessTarget, LocalPrincipal, ResourceKind, Role
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.domain.operations import (
    ActivateKillSwitchCommand,
    KillSwitchScope,
    ReconcilePaperCommand,
    ResumePaperCommand,
)
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope
from stonks_agent.entrypoints.cli_commands._local_auth import local_cli_principal
from stonks_agent.ports.paper_operations import (
    PaperOperationsUnitOfWork,
    PaperOperationsUnitOfWorkFactory,
)
from stonks_agent.ports.paper_projections import (
    PaperProjectionUnitOfWork,
    PaperProjectionUnitOfWorkFactory,
)

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _principal(
    target: AccessTarget | None,
    *,
    role: Role = Role.PAPER_OPERATOR,
) -> LocalPrincipal:
    return local_cli_principal(
        subject="local-paper-admin" if role is Role.ADMIN else "local-paper-operator",
        role=role,
        targets=frozenset() if target is None else frozenset({target}),
    )


@app.command("status")
def status_command(
    scope: Annotated[KillSwitchScope, typer.Option()],
    account_id: Annotated[str, typer.Option()] = "",
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Read one global or account paper kill switch."""
    principal = _principal(_scope_target(scope, account_id))
    _emit_result(
        _run_database(
            database_url,
            lambda unit_of_work: read_kill_switch(
                principal,
                scope,
                account_id or None,
                unit_of_work,
            ),
        )
    )


@app.command("actions")
def actions_command(
    after_sequence: Annotated[int, typer.Option(min=0)] = 0,
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Read the verified immutable operator action chain."""
    principal = _principal(None, role=Role.ADMIN)
    _emit_result(
        _run_database(
            database_url,
            lambda unit_of_work: read_operator_actions(
                principal,
                after_sequence=after_sequence,
                unit_of_work=unit_of_work,
            ),
        )
    )


@app.command("activate")
def activate_command(
    action_id: Annotated[str, typer.Option()],
    scope: Annotated[KillSwitchScope, typer.Option()],
    expected_version: Annotated[int, typer.Option(min=0)],
    reason_code: Annotated[str, typer.Option()],
    account_id: Annotated[str, typer.Option()] = "",
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Activate a switch and terminalize cancellable pending orders."""
    principal = _principal(_scope_target(scope, account_id))
    try:
        command = ActivateKillSwitchCommand(
            action_id=UUID(action_id),
            scope=scope,
            account_id=account_id or None,
            expected_version=expected_version,
            reason_code=reason_code,
            requested_at=datetime.now(UTC),
        )
    except (ValueError, ValidationError) as error:
        raise typer.BadParameter("paper activation input is invalid") from error
    _emit_result(
        _run_database(
            database_url,
            lambda unit_of_work: activate_kill_switch(principal, command, unit_of_work),
        )
    )


@app.command("reconcile")
def reconcile_command(
    action_id: Annotated[str, typer.Option()],
    account_id: Annotated[str, typer.Option()],
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Replay and audit one paper account."""
    principal = _principal(_account_target(account_id))
    try:
        command = ReconcilePaperCommand(
            action_id=UUID(action_id),
            account_id=account_id,
            requested_at=datetime.now(UTC),
        )
    except (ValueError, ValidationError) as error:
        raise typer.BadParameter("paper reconciliation input is invalid") from error
    _emit_result(
        _run_database(
            database_url,
            lambda unit_of_work: reconcile_paper_state(
                principal, command, unit_of_work
            ),
        )
    )


@app.command("resume")
def resume_command(
    action_id: Annotated[str, typer.Option()],
    scope: Annotated[KillSwitchScope, typer.Option()],
    expected_version: Annotated[int, typer.Option(min=1)],
    reason_code: Annotated[str, typer.Option()],
    account_id: Annotated[str, typer.Option()] = "",
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Disable a switch only after locked reconciliation passes."""
    principal = _principal(_scope_target(scope, account_id))
    try:
        command = ResumePaperCommand(
            action_id=UUID(action_id),
            scope=scope,
            account_id=account_id or None,
            expected_version=expected_version,
            reason_code=reason_code,
            requested_at=datetime.now(UTC),
        )
    except (ValueError, ValidationError) as error:
        raise typer.BadParameter("paper resume input is invalid") from error
    _emit_result(
        _run_database(
            database_url,
            lambda unit_of_work: resume_paper(principal, command, unit_of_work),
        )
    )


@app.command("portfolio")
def portfolio_command(
    account_id: Annotated[str, typer.Option()],
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Read the current settled/reserved/available portfolio projection."""
    principal = _principal(_account_target(account_id))
    _emit_result(
        _run_projection_database(
            database_url,
            lambda unit_of_work: read_portfolio_projection(
                principal, account_id, unit_of_work
            ),
        )
    )


@app.command("nav")
def nav_command(
    account_id: Annotated[str, typer.Option()],
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Read the latest valuation only when it matches the current ledger."""
    principal = _principal(_account_target(account_id))
    _emit_result(
        _run_projection_database(
            database_url,
            lambda unit_of_work: read_nav_projection(
                principal, account_id, unit_of_work
            ),
        )
    )


@app.command("risk")
def risk_command(
    account_id: Annotated[str, typer.Option()],
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Read the latest risk decision as a non-authoritative projection."""
    principal = _principal(_account_target(account_id))
    _emit_result(
        _run_projection_database(
            database_url,
            lambda unit_of_work: read_risk_projection(
                principal,
                account_id,
                as_of=datetime.now(UTC),
                unit_of_work=unit_of_work,
            ),
        )
    )


def _run_database[T](
    database_url: str,
    operation: Callable[[PaperOperationsUnitOfWorkFactory], Result[T]],
) -> Result[T]:
    if not database_url:
        raise typer.BadParameter("STONKS_DATABASE_URL is required")
    engine = create_engine(database_url, pool_pre_ping=True)

    def factory() -> PaperOperationsUnitOfWork:
        return cast(PaperOperationsUnitOfWork, PostgresUnitOfWork(engine))

    try:
        return operation(factory)
    finally:
        engine.dispose()


def _scope_target(
    scope: KillSwitchScope,
    account_id: str,
) -> AccessTarget | None:
    if scope is KillSwitchScope.GLOBAL and not account_id:
        return AccessTarget(kind=ResourceKind.PAPER_GLOBAL, identifier="global")
    if scope is KillSwitchScope.ACCOUNT and account_id:
        return _account_target(account_id)
    return None


def _account_target(account_id: str) -> AccessTarget:
    return AccessTarget(kind=ResourceKind.ACCOUNT, identifier=account_id)


def _run_projection_database[T](
    database_url: str,
    operation: Callable[[PaperProjectionUnitOfWorkFactory], Result[T]],
) -> Result[T]:
    if not database_url:
        raise typer.BadParameter("STONKS_DATABASE_URL is required")
    engine = create_engine(database_url, pool_pre_ping=True)

    def factory() -> PaperProjectionUnitOfWork:
        return cast(PaperProjectionUnitOfWork, PostgresUnitOfWork(engine))

    try:
        return operation(factory)
    finally:
        engine.dispose()


def _emit_result[T](result: Result[T]) -> None:
    if isinstance(result, Failure):
        _emit(error_envelope(result.error))
        raise typer.Exit(code=2)
    _emit(success_envelope(result.value))


def _emit(value: BaseModel) -> None:
    typer.echo(
        json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    )
