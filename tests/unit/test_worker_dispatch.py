from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from typer.testing import CliRunner

import stonks_agent.entrypoints.worker as worker_entrypoint
from stonks_agent.domain.errors import ErrorCode, Result, Success
from stonks_agent.domain.job import FailJob, JobFailureReceipt, JobLease
from stonks_agent.entrypoints.worker import app, dispatch_worker_job, run_worker_once
from stonks_agent.ports.queue import QueuePort

NOW = datetime(2026, 7, 28, 8, tzinfo=UTC)
JOB_ID = UUID("73000000-0000-4000-8000-000000000001")
RUN_ID = UUID("73000000-0000-4000-8000-000000000002")
EVENT_ID = UUID("73000000-0000-4000-8000-000000000003")
OUTBOX_ID = UUID("73000000-0000-4000-8000-000000000004")


class RecordingFailureQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[FailJob, datetime]] = []
        self.receipt = JobFailureReceipt(
            job_id=JOB_ID,
            run_id=RUN_ID,
            event_id=EVENT_ID,
            outbox_id=OUTBOX_ID,
            sequence=2,
            error_code=ErrorCode.CAPABILITY_DENIED,
            reason_code="unknown_job_type",
            failed_at=NOW,
        )

    def fail(self, request: FailJob, *, now: datetime) -> Result[JobFailureReceipt]:
        self.calls.append((request, now))
        return Success(self.receipt)


class ClaimQueue(RecordingFailureQueue):
    def __init__(self, lease: JobLease | None) -> None:
        super().__init__()
        self.lease = lease

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> Result[JobLease]:
        del worker_id, now, lease_for
        if self.lease is None:
            from stonks_agent.domain.errors import Failure, StructuredError

            return Failure(
                StructuredError(
                    code=ErrorCode.NOT_FOUND,
                    message="No job available",
                )
            )
        return Success(self.lease)


def test_dispatcher_calls_only_the_exact_registered_handler() -> None:
    queue = RecordingFailureQueue()
    lease = job_lease("research_pipeline")
    handled: list[JobLease] = []

    def handler(candidate: JobLease) -> Result[object]:
        handled.append(candidate)
        return Success({"status": "handled"})

    result = dispatch_worker_job(
        lease,
        handlers={"research_pipeline": handler},
        queue=cast(QueuePort, queue),
        now=NOW,
    )

    assert result == Success({"status": "handled"})
    assert handled == [lease]
    assert queue.calls == []


def test_worker_once_claims_binds_and_dispatches_one_job() -> None:
    lease = job_lease("research_pipeline")
    queue = ClaimQueue(lease)
    handled: list[JobLease] = []

    def handle(candidate: JobLease) -> Result[object]:
        handled.append(candidate)
        return Success("done")

    result = run_worker_once(
        cast(QueuePort, queue),
        handlers={"research_pipeline": handle},
        worker_id="worker-a",
        now=NOW,
        lease_for=timedelta(seconds=30),
    )

    assert result == Success(True)
    assert handled == [lease]


def test_worker_once_reports_idle_without_dispatch() -> None:
    queue = ClaimQueue(None)

    result = run_worker_once(
        cast(QueuePort, queue),
        handlers={},
        worker_id="worker-a",
        now=NOW,
        lease_for=timedelta(seconds=30),
    )

    assert result == Success(False)


def test_continuous_worker_rejects_a_lease_shorter_than_handler_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composed = False

    def unexpected_composition(**_: object) -> object:
        nonlocal composed
        composed = True
        raise AssertionError("unsafe lease reached runtime composition")

    monkeypatch.setattr(
        worker_entrypoint,
        "build_local_runtime",
        unexpected_composition,
    )
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--worker-id",
            "worker-a",
            "--database-url",
            "postgresql+psycopg://invalid.invalid/unused",
            "--lease-seconds",
            "599",
            "--max-jobs",
            "1",
        ],
    )

    assert result.exit_code == 2
    assert "600" in result.output
    assert composed is False


def test_dispatcher_preserves_snapshot_dedicated_handler_boundary() -> None:
    queue = RecordingFailureQueue()
    lease = job_lease("create_snapshot")

    result = dispatch_worker_job(
        lease,
        handlers={"create_snapshot": lambda _: Success("canonical-snapshot-handler")},
        queue=cast(QueuePort, queue),
        now=NOW,
    )

    assert result == Success("canonical-snapshot-handler")
    assert queue.calls == []


@pytest.mark.parametrize(
    "job_type",
    (
        "RESEARCH_PIPELINE",
        "research_pipeline ",
        " research_pipeline",
        "unknown",
    ),
)
def test_dispatcher_dead_letters_unknown_or_near_match_job_type(
    job_type: str,
) -> None:
    queue = RecordingFailureQueue()
    lease = job_lease(job_type)
    called = False

    def handler(_: JobLease) -> Result[object]:
        nonlocal called
        called = True
        return Success("unexpected")

    result = dispatch_worker_job(
        lease,
        handlers={"research_pipeline": handler},
        queue=cast(QueuePort, queue),
        now=NOW,
    )

    assert result == Success(queue.receipt)
    assert called is False
    assert len(queue.calls) == 1
    request, failed_at = queue.calls[0]
    assert request.job_id == lease.job_id
    assert request.worker_id == lease.lease_owner
    assert request.attempt_generation == lease.attempt_generation
    assert request.attempt_nonce == lease.attempt_nonce
    assert request.error_code is ErrorCode.CAPABILITY_DENIED
    assert request.reason_code == "unknown_job_type"
    assert failed_at == NOW


def job_lease(job_type: str) -> JobLease:
    return JobLease(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type=job_type,
        payload={"snapshot_id": "snapshot-1"},
        attempt_generation=2,
        attempt_nonce="nonce-exact",
        lease_owner="worker-a",
        lease_until=NOW + timedelta(seconds=30),
        attempts=2,
        deadline_at=NOW + timedelta(minutes=5),
    )
