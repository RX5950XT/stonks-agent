"""Durable worker CLI that claims one fenced PostgreSQL job."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer
from pydantic import BaseModel
from sqlalchemy import create_engine

from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.domain.errors import ErrorCode, Failure
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.callback()
def main() -> None:
    """Stonks Agent durable worker commands."""


@app.command("claim-once")
def claim_once(
    worker_id: Annotated[
        str,
        typer.Option(help="穩定且不含秘密的 worker identity"),
    ],
    database_url: Annotated[
        str,
        typer.Option(
            envvar="STONKS_DATABASE_URL",
            help="由環境注入的 PostgreSQL URL; 不會寫入輸出",
        ),
    ],
    lease_seconds: Annotated[
        int,
        typer.Option(min=1, max=3600, help="Lease 秒數"),
    ] = 30,
) -> None:
    """Claim one job; processing remains in a typed worker handler."""
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        result = PostgresJobQueue(engine).claim(
            worker_id=worker_id,
            now=datetime.now(UTC),
            lease_for=timedelta(seconds=lease_seconds),
        )
    finally:
        engine.dispose()
    if isinstance(result, Failure):
        if result.error.code is ErrorCode.NOT_FOUND:
            _emit(success_envelope({"claimed": False, "lease": None}))
            return
        _emit(error_envelope(result.error))
        raise typer.Exit(code=2)
    _emit(
        success_envelope(
            {
                "claimed": True,
                "lease": result.value.model_dump(mode="json"),
            }
        )
    )


def _emit(value: BaseModel) -> None:
    payload = value.model_dump(mode="json")
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    app()
