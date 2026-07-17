"""Durable worker CLI that claims one fenced PostgreSQL job."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer
from pydantic import BaseModel
from sqlalchemy import create_engine

from stonks_agent.adapters.observability.context import (
    TraceIdGenerator,
    create_trace_context,
    current_trace_context,
)
from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.domain.errors import ErrorCode, Failure, Result
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.telemetry import (
    ComponentName,
    CorrelationContext,
    OperationName,
    TraceContext,
)
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope
from stonks_agent.ports.queue import QueuePort
from stonks_agent.ports.telemetry import OperationRecorderPort

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
        result = claim_worker_job(
            PostgresJobQueue(engine),
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
                "lease": public_lease_payload(result.value),
            }
        )
    )


def worker_context_for_lease(
    lease: JobLease,
    *,
    generator: TraceIdGenerator | None = None,
) -> TraceContext:
    return create_trace_context(
        parent=lease.trace_carrier,
        correlation=CorrelationContext(
            request_id=lease.correlation_id,
            run_id=str(lease.run_id),
            job_id=str(lease.job_id),
        ),
        generator=generator,
    )


def claim_worker_job(
    queue: QueuePort,
    *,
    worker_id: str,
    now: datetime,
    lease_for: timedelta,
    recorder: OperationRecorderPort | None = None,
) -> Result[JobLease]:
    def call() -> Result[JobLease]:
        return queue.claim(
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
        )

    if recorder is None:
        return call()
    return _record_worker_claim(recorder, call)


def _record_worker_claim(
    recorder: OperationRecorderPort,
    call: Callable[[], Result[JobLease]],
) -> Result[JobLease]:
    captured: list[Result[JobLease]] = []
    raised: list[BaseException] = []
    executed = False

    def invoke() -> Result[JobLease]:
        nonlocal executed
        if executed:
            if raised:
                raise raised[0]
            return captured[0]
        executed = True
        try:
            result = call()
        except BaseException as error:
            raised.append(error)
            raise
        captured.append(result)
        return result

    with suppress(Exception):
        recorder.record_result(
            component=ComponentName.WORKER,
            operation=OperationName.CLAIM,
            call=invoke,
            parent=current_trace_context(),
        )
    return invoke()


def public_lease_payload(lease: JobLease) -> dict[str, object]:
    return lease.model_dump(
        mode="json",
        exclude={"trace_carrier", "correlation_id"},
    )


def _emit(value: BaseModel) -> None:
    payload = value.model_dump(mode="json")
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    app()
