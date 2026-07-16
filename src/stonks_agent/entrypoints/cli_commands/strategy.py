"""Local strategy registry review, audit, and paper-only transition commands."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import typer
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine

from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.application.strategies.manage import (
    read_evaluation,
    read_strategy,
    read_strategy_events,
    transition_strategy,
)
from stonks_agent.domain.auth import AccessTarget, LocalPrincipal, ResourceKind, Role
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.domain.strategy import PromotionState, StrategyTransitionRequest
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope
from stonks_agent.entrypoints.cli_commands._local_auth import local_cli_principal
from stonks_agent.ports.strategy_registry import (
    StrategyUnitOfWork,
    StrategyUnitOfWorkFactory,
)

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _principal(target: AccessTarget) -> LocalPrincipal:
    return local_cli_principal(
        subject="local-reviewer",
        role=Role.STRATEGY_REVIEWER,
        targets=frozenset({target}),
    )


@app.command("show")
def show_command(
    strategy_id: Annotated[str, typer.Option()],
    strategy_version: Annotated[str, typer.Option()],
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Read one exact strategy registry version."""
    strategy_id, strategy_version = _validated_identity(strategy_id, strategy_version)
    principal = _strategy_principal(strategy_id, strategy_version)
    _emit_result(
        _run_database(
            database_url,
            lambda unit_of_work: read_strategy(
                principal,
                strategy_id,
                strategy_version,
                unit_of_work,
            ),
        )
    )


@app.command("events")
def events_command(
    strategy_id: Annotated[str, typer.Option()],
    strategy_version: Annotated[str, typer.Option()],
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Read the verified immutable strategy audit chain."""
    strategy_id, strategy_version = _validated_identity(strategy_id, strategy_version)
    principal = _strategy_principal(strategy_id, strategy_version)
    _emit_result(
        _run_database(
            database_url,
            lambda unit_of_work: read_strategy_events(
                principal,
                strategy_id,
                strategy_version,
                unit_of_work,
            ),
        )
    )


@app.command("evaluation")
def evaluation_command(
    report_id: Annotated[str, typer.Option()],
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Read one immutable evaluation report and its content hash."""
    try:
        identifier = UUID(report_id)
        principal = _principal(
            AccessTarget(kind=ResourceKind.EVALUATION, identifier=str(identifier))
        )
    except ValueError as error:
        raise typer.BadParameter("report-id is invalid") from error
    result = _run_database(
        database_url,
        lambda unit_of_work: read_evaluation(principal, identifier, unit_of_work),
    )
    if isinstance(result, Failure):
        _emit_result(result)
        return
    payload = result.value.model_dump(mode="json") | {
        "evaluation_hash": result.value.evaluation_hash
    }
    _emit(success_envelope(payload))


@app.command("transition")
def transition_command(
    strategy_id: Annotated[str, typer.Option()],
    strategy_version: Annotated[str, typer.Option()],
    expected_version: Annotated[int, typer.Option(min=1)],
    current_state: Annotated[PromotionState, typer.Option()],
    target_state: Annotated[PromotionState, typer.Option()],
    reason_code: Annotated[str, typer.Option()],
    evaluation_report_id: Annotated[str, typer.Option()] = "",
    evaluation_hash: Annotated[str, typer.Option()] = "",
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    """Apply one reviewer-authorized CAS transition in the paper-only graph."""
    try:
        validated_id, validated_version = _validated_identity(
            strategy_id,
            strategy_version,
        )
        principal = _strategy_principal(validated_id, validated_version)
        command = StrategyTransitionRequest(
            strategy_id=validated_id,
            strategy_version=validated_version,
            expected_version=expected_version,
            current_state=current_state,
            target_state=target_state,
            evaluation_report_id=(
                UUID(evaluation_report_id) if evaluation_report_id else None
            ),
            evaluation_hash=evaluation_hash or None,
            reason_code=reason_code,
            actor=principal.subject,
            requested_at=datetime.now(UTC),
        )
    except (ValueError, ValidationError) as error:
        raise typer.BadParameter("strategy transition input is invalid") from error
    _emit_result(
        _run_database(
            database_url,
            lambda unit_of_work: transition_strategy(principal, command, unit_of_work),
        )
    )


def _run_database[T](
    database_url: str,
    operation: Callable[[StrategyUnitOfWorkFactory], Result[T]],
) -> Result[T]:
    if not database_url:
        raise typer.BadParameter("STONKS_DATABASE_URL is required")
    engine = create_engine(database_url, pool_pre_ping=True)

    def factory() -> StrategyUnitOfWork:
        return PostgresUnitOfWork(engine)

    try:
        return operation(factory)
    finally:
        engine.dispose()


def _validated_identity(strategy_id: str, strategy_version: str) -> tuple[str, str]:
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", strategy_id):
        raise typer.BadParameter("strategy-id is invalid")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", strategy_version):
        raise typer.BadParameter("strategy-version is invalid")
    return strategy_id, strategy_version


def _strategy_principal(strategy_id: str, strategy_version: str) -> LocalPrincipal:
    return _principal(
        AccessTarget(
            kind=ResourceKind.STRATEGY,
            identifier=f"{strategy_id}@{strategy_version}",
        )
    )


def _emit_result[T](result: Result[T]) -> None:
    if isinstance(result, Failure):
        _emit(error_envelope(result.error))
        raise typer.Exit(code=2)
    _emit(success_envelope(result.value))


def _emit(value: BaseModel) -> None:
    typer.echo(
        json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    )
