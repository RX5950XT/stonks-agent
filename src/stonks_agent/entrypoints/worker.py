"""Durable worker CLI that claims one fenced PostgreSQL job."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel
from sqlalchemy import create_engine

from stonks_agent.adapters.observability.context import (
    TraceIdGenerator,
    create_trace_context,
    current_trace_context,
    trace_scope,
)
from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.composition.runtime import build_local_runtime
from stonks_agent.composition.worker import build_worker_composition
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.job import FailJob, JobFailureReceipt, JobLease
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
type WorkerJobHandler = Callable[[JobLease], Result[object]]
type WorkerDispatchResult = Result[object] | Result[JobFailureReceipt]
MIN_PROCESSING_LEASE_SECONDS = 600


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


@app.command("run")
def run(
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
    artifact_root: Annotated[
        Path,
        typer.Option(envvar="STONKS_ARTIFACT_ROOT"),
    ] = Path(".data/artifacts"),
    lease_seconds: Annotated[
        int,
        typer.Option(min=MIN_PROCESSING_LEASE_SECONDS, max=3600),
    ] = MIN_PROCESSING_LEASE_SECONDS,
    poll_seconds: Annotated[
        float,
        typer.Option(min=0.05, max=10),
    ] = 0.5,
    max_jobs: Annotated[
        int,
        typer.Option(min=0, help="0 代表持續執行"),
    ] = 0,
) -> None:
    """Continuously claim and execute exact registered worker jobs."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}", worker_id) is None:
        raise typer.BadParameter("worker identity is invalid")
    runtime = build_local_runtime(
        database_url=database_url,
        artifact_root=artifact_root,
    )
    try:
        composition = build_worker_composition(
            runtime,
            environment=os.environ,
        )
        processed = _run_loop(
            composition.queue,
            handlers=composition.handlers,
            worker_id=worker_id,
            lease_for=timedelta(seconds=lease_seconds),
            poll_seconds=poll_seconds,
            max_jobs=max_jobs,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        _emit(
            error_envelope(
                _worker_configuration_error(),
            )
        )
        raise typer.Exit(code=2) from None
    finally:
        runtime.close()
    _emit(success_envelope({"processed": processed}))


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


def dispatch_worker_job(
    lease: JobLease,
    *,
    handlers: Mapping[str, WorkerJobHandler],
    queue: QueuePort,
    now: datetime,
) -> WorkerDispatchResult:
    """Dispatch one exact job type or terminally reject the fenced lease."""

    handler = handlers.get(lease.job_type)
    if handler is not None:
        return handler(lease)
    return queue.fail(
        FailJob(
            job_id=lease.job_id,
            worker_id=lease.lease_owner,
            attempt_generation=lease.attempt_generation,
            attempt_nonce=lease.attempt_nonce,
            error_code=ErrorCode.CAPABILITY_DENIED,
            reason_code="unknown_job_type",
        ),
        now=now,
    )


def run_worker_once(
    queue: QueuePort,
    *,
    handlers: Mapping[str, WorkerJobHandler],
    worker_id: str,
    now: datetime,
    lease_for: timedelta,
    recorder: OperationRecorderPort | None = None,
) -> Result[bool]:
    claimed = claim_worker_job(
        queue,
        worker_id=worker_id,
        now=now,
        lease_for=lease_for,
        recorder=recorder,
    )
    if isinstance(claimed, Failure):
        return Success(False) if claimed.error.code is ErrorCode.NOT_FOUND else claimed
    lease = claimed.value
    with trace_scope(worker_context_for_lease(lease)):
        dispatched = dispatch_worker_job(
            lease,
            handlers=handlers,
            queue=queue,
            now=now,
        )
    if isinstance(dispatched, Failure):
        return dispatched
    return Success(True)


def _run_loop(
    queue: QueuePort,
    *,
    handlers: Mapping[str, WorkerJobHandler],
    worker_id: str,
    lease_for: timedelta,
    poll_seconds: float,
    max_jobs: int,
) -> int:
    processed = 0
    while max_jobs == 0 or processed < max_jobs:
        outcome = run_worker_once(
            queue,
            handlers=handlers,
            worker_id=worker_id,
            now=datetime.now(UTC),
            lease_for=lease_for,
        )
        if isinstance(outcome, Success) and outcome.value:
            processed += 1
            continue
        time.sleep(poll_seconds)
    return processed


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
        exclude={"trace_carrier", "correlation_id", "attempt_nonce"},
    )


def _emit(value: BaseModel) -> None:
    payload = value.model_dump(mode="json")
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _worker_configuration_error() -> StructuredError:
    return StructuredError(
        code=ErrorCode.CONFIGURATION_INVALID,
        message="Worker configuration is invalid",
    )


if __name__ == "__main__":
    app()
