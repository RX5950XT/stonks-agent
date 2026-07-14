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
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.domain.operations import (
    ActivateKillSwitchCommand,
    KillSwitchScope,
    ReconcilePaperCommand,
    ResumePaperCommand,
)
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope
from stonks_agent.ports.paper_operations import (
    PaperOperationsUnitOfWork,
    PaperOperationsUnitOfWorkFactory,
)

app = typer.Typer(add_completion=False, no_args_is_help=True)
_PRINCIPAL = LocalPrincipal(
    subject="local-paper-operator",
    roles=frozenset({Role.PAPER_OPERATOR}),
)


@app.command("status")
def status_command(
    scope: Annotated[KillSwitchScope, typer.Option()],
    account_id: Annotated[str, typer.Option()] = "",
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Read one global or account paper kill switch."""
    _emit_result(
        _run_database(
            database_url,
            lambda unit_of_work: read_kill_switch(
                _PRINCIPAL,
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
    _emit_result(
        _run_database(
            database_url,
            lambda unit_of_work: read_operator_actions(
                _PRINCIPAL,
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
            lambda unit_of_work: activate_kill_switch(
                _PRINCIPAL, command, unit_of_work
            ),
        )
    )


@app.command("reconcile")
def reconcile_command(
    action_id: Annotated[str, typer.Option()],
    account_id: Annotated[str, typer.Option()],
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Replay and audit one paper account."""
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
                _PRINCIPAL, command, unit_of_work
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
            lambda unit_of_work: resume_paper(_PRINCIPAL, command, unit_of_work),
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


def _emit_result[T](result: Result[T]) -> None:
    if isinstance(result, Failure):
        _emit(error_envelope(result.error))
        raise typer.Exit(code=2)
    _emit(success_envelope(result.value))


def _emit(value: BaseModel) -> None:
    typer.echo(
        json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    )
