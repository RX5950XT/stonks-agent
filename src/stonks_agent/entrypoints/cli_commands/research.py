"""CLI commands for queued research runs and canonical event reads."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import typer
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine

from stonks_agent.adapters.postgres.research_query import (
    PostgresResearchRequestStore,
    PostgresRunEventReader,
)
from stonks_agent.application.research.request_run import (
    read_run_events,
    request_research_run,
)
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.domain.research_run import ResearchRunRequest
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope

app = typer.Typer(add_completion=False, no_args_is_help=True)
_PRINCIPAL = LocalPrincipal(subject="local-cli", roles=frozenset({Role.RESEARCHER}))


@app.command("request")
def request_command(
    instrument_id: Annotated[str, typer.Option()] = "instrument-aapl",
    symbol: Annotated[str, typer.Option()] = "AAPL",
    as_of: Annotated[str, typer.Option()] = "2026-01-02T21:00:00Z",
    snapshot_id: Annotated[str, typer.Option()] = "",
    research_profile_id: Annotated[str, typer.Option()] = "balanced/1",
    model_policy_id: Annotated[str, typer.Option()] = "research-models/1",
    language: Annotated[str, typer.Option()] = "zh-TW",
    idempotency_key: Annotated[str, typer.Option()] = "cli-research",
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    if not database_url:
        raise typer.BadParameter("STONKS_DATABASE_URL is required")
    try:
        command = ResearchRunRequest(
            instrument_id=instrument_id,
            symbol=symbol,
            as_of=datetime.fromisoformat(as_of),
            snapshot_id=UUID(snapshot_id),
            research_profile_id=research_profile_id,
            model_policy_id=model_policy_id,
            language=language,
            idempotency_key=idempotency_key,
            requested_at=datetime.now(UTC),
        )
    except (ValueError, ValidationError) as error:
        raise typer.BadParameter("research request input is invalid") from error
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        result = request_research_run(
            _PRINCIPAL, command, PostgresResearchRequestStore(engine)
        )
    finally:
        engine.dispose()
    _emit_result(result, status=202)


@app.command("events")
def events_command(
    run_id: Annotated[str, typer.Option()],
    after_sequence: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
    database_url: Annotated[str, typer.Option(envvar="STONKS_DATABASE_URL")] = "",
) -> None:
    if not database_url:
        raise typer.BadParameter("STONKS_DATABASE_URL is required")
    try:
        identifier = UUID(run_id)
    except ValueError as error:
        raise typer.BadParameter("run-id is invalid") from error
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        result = read_run_events(
            _PRINCIPAL,
            identifier,
            after_sequence=after_sequence,
            limit=limit,
            reader=PostgresRunEventReader(engine),
        )
    finally:
        engine.dispose()
    _emit_result(result)


def _emit_result[T](result: Result[T], *, status: int = 200) -> None:
    if isinstance(result, Failure):
        _emit(error_envelope(result.error))
        raise typer.Exit(code=2)
    _emit(success_envelope(result.value, status=status))


def _emit(value: BaseModel) -> None:
    typer.echo(
        json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    )
