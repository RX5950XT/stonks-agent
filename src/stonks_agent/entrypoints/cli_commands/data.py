"""CLI commands for durable snapshot ingestion requests."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine

from stonks_agent.adapters.postgres.snapshot_requests import (
    PostgresSnapshotRequestStore,
)
from stonks_agent.application.data.create_snapshot import request_snapshot
from stonks_agent.domain.auth import Role
from stonks_agent.domain.errors import Failure
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope
from stonks_agent.entrypoints.cli_commands._local_auth import local_cli_principal

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command("request-snapshot")
def request_snapshot_command(
    market: Annotated[str, typer.Option()] = "US",
    capability: Annotated[str, typer.Option()] = "prices",
    symbol: Annotated[str, typer.Option()] = "AAPL",
    as_of: Annotated[str, typer.Option()] = "",
    query_json: Annotated[str, typer.Option()] = "",
    provider_policy_id: Annotated[str, typer.Option()] = "us-prices/1",
    idempotency_key: Annotated[str, typer.Option()] = "cli-snapshot",
    database_url: Annotated[
        str,
        typer.Option(
            envvar="STONKS_DATABASE_URL", help="PostgreSQL URL from environment"
        ),
    ] = "",
) -> None:
    principal = local_cli_principal(subject="local-cli", role=Role.RESEARCHER)
    if not database_url:
        raise typer.BadParameter("STONKS_DATABASE_URL is required")
    try:
        requested_at = datetime.now(UTC)
        parsed_query = _query(symbol, query_json)
        decision_time = (
            datetime.fromisoformat(as_of)
            if as_of
            else requested_at + timedelta(minutes=15)
        )
        request = CreateSnapshotRequest(
            market=market,
            capability=capability,
            as_of=decision_time,
            query=parsed_query,
            provider_policy_id=provider_policy_id,
            idempotency_key=idempotency_key,
            owner_subject=principal.subject,
            requested_at=requested_at,
        )
    except (ValueError, ValidationError, json.JSONDecodeError) as error:
        raise typer.BadParameter("snapshot request input is invalid") from error
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


def _query(symbol: str, query_json: str) -> dict[str, object]:
    if query_json:
        parsed = json.loads(query_json)
        if not isinstance(parsed, dict):
            raise ValueError
        return parsed
    normalized = symbol.strip().upper()
    if re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,15}", normalized) is None:
        raise ValueError
    return {"symbol": normalized}


def _emit(value: BaseModel) -> None:
    typer.echo(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
