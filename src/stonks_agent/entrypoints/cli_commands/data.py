"""CLI commands for durable snapshot ingestion requests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

import typer
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine

from stonks_agent.adapters.postgres.snapshot_requests import (
    PostgresSnapshotRequestStore,
)
from stonks_agent.application.data.create_snapshot import request_snapshot
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import Failure
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command("request-snapshot")
def request_snapshot_command(
    market: Annotated[str, typer.Option()] = "US",
    capability: Annotated[str, typer.Option()] = "prices",
    as_of: Annotated[str, typer.Option()] = "2026-01-02T21:00:00Z",
    query_json: Annotated[str, typer.Option()] = '{"symbol":"AAPL"}',
    provider_policy_id: Annotated[str, typer.Option()] = "us-prices/1",
    idempotency_key: Annotated[str, typer.Option()] = "cli-snapshot",
    database_url: Annotated[
        str,
        typer.Option(
            envvar="STONKS_DATABASE_URL", help="PostgreSQL URL from environment"
        ),
    ] = "",
) -> None:
    if not database_url:
        raise typer.BadParameter("STONKS_DATABASE_URL is required")
    try:
        parsed_query = json.loads(query_json)
        if not isinstance(parsed_query, dict):
            raise ValueError
        decision_time = datetime.fromisoformat(as_of)
        request = CreateSnapshotRequest(
            market=market,
            capability=capability,
            as_of=decision_time,
            query=parsed_query,
            provider_policy_id=provider_policy_id,
            idempotency_key=idempotency_key,
            requested_at=datetime.now(UTC),
        )
    except (ValueError, ValidationError, json.JSONDecodeError) as error:
        raise typer.BadParameter("snapshot request input is invalid") from error
    principal = LocalPrincipal(
        subject="local-cli",
        roles=frozenset({Role.RESEARCHER}),
    )
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        result = request_snapshot(
            principal,
            request,
            PostgresSnapshotRequestStore(engine),
        )
    finally:
        engine.dispose()
    if isinstance(result, Failure):
        _emit(error_envelope(result.error))
        raise typer.Exit(code=2)
    _emit(success_envelope(result.value, status=202))


def _emit(value: BaseModel) -> None:
    typer.echo(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
