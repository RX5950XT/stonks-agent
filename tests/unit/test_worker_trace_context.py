from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from stonks_agent.domain.errors import Result, Success
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.telemetry import (
    ComponentName,
    OperationName,
    TraceCarrier,
    TraceContext,
)
from stonks_agent.entrypoints.worker import (
    claim_worker_job,
    public_lease_payload,
    worker_context_for_lease,
)
from stonks_agent.ports.queue import QueuePort

NOW = datetime(2026, 7, 17, 8, tzinfo=UTC)
RUN_ID = UUID("c0000000-0000-4000-8000-000000000001")
JOB_ID = UUID("c0000000-0000-4000-8000-000000000002")
TRACE = TraceCarrier(
    traceparent="00-11111111111111111111111111111111-2222222222222222-01",
    tracestate="vendor=value",
)


class FixedGenerator:
    def new_trace_id(self) -> str:
        return "a" * 32

    def new_span_id(self) -> str:
        return "b" * 16


class ClaimQueue:
    def __init__(self) -> None:
        self.calls = 0
        self.result = Success(_lease())

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> Result[JobLease]:
        del worker_id, now, lease_for
        self.calls += 1
        return self.result


class WorkerRecorder:
    def __init__(self) -> None:
        self.operations: list[tuple[ComponentName, OperationName]] = []

    def record_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Result[T]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        del parent
        first = call()
        assert call() is first
        self.operations.append((component, operation))
        return first

    async def record_async_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Awaitable[Result[T]]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        del component, operation, parent
        return await call()


class ForgingWorkerRecorder(WorkerRecorder):
    def __init__(self, *, skip_call: bool) -> None:
        super().__init__()
        self.skip_call = skip_call

    def record_result[T](
        self,
        *,
        component: ComponentName,
        operation: OperationName,
        call: Callable[[], Result[T]],
        parent: TraceContext | None = None,
    ) -> Result[T]:
        del component, operation, parent
        if not self.skip_call:
            call()
        forged = Success(_lease().model_copy(update={"attempts": 2}))
        return cast(Result[T], forged)


def test_worker_context_continues_lease_trace_with_new_span_and_job_correlation() -> (
    None
):
    context = worker_context_for_lease(_lease(), generator=FixedGenerator())

    assert context.trace_id == "1" * 32
    assert context.span_id == "b" * 16
    assert context.trace_flags == "01"
    assert context.tracestate == "vendor=value"
    assert context.request_id == "request-worker-1"
    assert context.run_id == str(RUN_ID)
    assert context.job_id == str(JOB_ID)


def test_worker_context_starts_new_trace_when_lease_has_no_carrier() -> None:
    context = worker_context_for_lease(
        _lease().model_copy(update={"trace_carrier": None, "correlation_id": None}),
        generator=FixedGenerator(),
    )

    assert context.trace_id == "a" * 32
    assert context.span_id == "b" * 16
    assert context.request_id is None


def test_worker_cli_payload_never_exposes_trace_correlation_or_nonce() -> None:
    lease = _lease()
    payload = public_lease_payload(lease)

    assert "trace_carrier" not in payload
    assert "correlation_id" not in payload
    assert "attempt_nonce" not in payload
    assert "traceparent" not in str(payload)
    assert lease.attempt_nonce not in str(payload)
    assert lease.attempt_nonce not in repr(lease)
    assert payload["job_id"] == str(JOB_ID)


def test_worker_claim_boundary_is_instrumented_and_exactly_once() -> None:
    queue = ClaimQueue()
    recorder = WorkerRecorder()

    result = claim_worker_job(
        cast(QueuePort, queue),
        worker_id="worker-a",
        now=NOW,
        lease_for=timedelta(seconds=30),
        recorder=recorder,
    )

    assert isinstance(result, Success)
    assert queue.calls == 1
    assert recorder.operations == [(ComponentName.WORKER, OperationName.CLAIM)]


def test_worker_recorder_cannot_skip_or_replace_claim_result() -> None:
    for skip_call in (False, True):
        queue = ClaimQueue()

        result = claim_worker_job(
            cast(QueuePort, queue),
            worker_id="worker-a",
            now=NOW,
            lease_for=timedelta(seconds=30),
            recorder=ForgingWorkerRecorder(skip_call=skip_call),
        )

        assert result is queue.result
        assert queue.calls == 1


def _lease() -> JobLease:
    return JobLease(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type="research",
        payload={"snapshot_id": "snapshot-1"},
        attempt_generation=1,
        attempt_nonce="opaque-fence",
        lease_owner="worker-a",
        lease_until=NOW + timedelta(seconds=30),
        attempts=1,
        deadline_at=NOW + timedelta(minutes=5),
        trace_carrier=TRACE,
        correlation_id="request-worker-1",
    )
