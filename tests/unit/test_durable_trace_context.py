from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.domain.inbox import InboxMessage, InboxReceipt
from stonks_agent.domain.job import EnqueueJob, JobLease, JobRecord, JobStatus
from stonks_agent.domain.outbox import OutboxLease
from stonks_agent.domain.telemetry import TraceCarrier

NOW = datetime(2026, 7, 17, 8, tzinfo=UTC)
RUN_ID = UUID("a0000000-0000-4000-8000-000000000001")
JOB_ID = UUID("a0000000-0000-4000-8000-000000000002")
OUTBOX_ID = UUID("a0000000-0000-4000-8000-000000000003")
LEASE_NONCE = UUID("a0000000-0000-4000-8000-000000000004")
TRACE = TraceCarrier(
    traceparent="00-11111111111111111111111111111111-2222222222222222-01",
    tracestate="vendor=value",
)


def test_enqueue_trace_context_is_optional_and_does_not_change_payload_hash() -> None:
    without_trace = _enqueue()
    with_trace = _enqueue(
        trace_carrier=TRACE,
        correlation_id="request-7f8f3d",
    )

    assert without_trace.payload_hash == with_trace.payload_hash
    assert without_trace.trace_carrier is None
    assert without_trace.correlation_id is None
    assert with_trace.trace_carrier == TRACE


@pytest.mark.parametrize("value", ("", "-bad", "x" * 129, "white space"))
def test_durable_correlation_id_is_bounded(value: str) -> None:
    with pytest.raises(ValidationError):
        _enqueue(correlation_id=value)


def test_job_record_and_lease_expose_transport_context_without_changing_fence() -> None:
    record = JobRecord(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type="research",
        payload={"snapshot": "one"},
        payload_hash="a" * 64,
        status=JobStatus.QUEUED,
        idempotency_key="job-key",
        not_before=NOW,
        deadline_at=NOW + timedelta(minutes=5),
        attempts=0,
        max_attempts=3,
        attempt_generation=0,
        trace_carrier=TRACE,
        correlation_id="request-7f8f3d",
        created_at=NOW,
        updated_at=NOW,
    )
    lease = JobLease(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type="research",
        payload=record.payload,
        attempt_generation=1,
        attempt_nonce="opaque-fence",
        lease_owner="worker-a",
        lease_until=NOW + timedelta(seconds=30),
        attempts=1,
        deadline_at=record.deadline_at,
        trace_carrier=record.trace_carrier,
        correlation_id=record.correlation_id,
    )

    assert lease.trace_carrier == TRACE
    assert lease.correlation_id == "request-7f8f3d"
    assert lease.attempt_generation == 1
    assert lease.attempt_nonce == "opaque-fence"


def test_outbox_and_inbox_expose_transport_context_without_hashing_it() -> None:
    lease = OutboxLease(
        outbox_id=OUTBOX_ID,
        aggregate_type="run",
        aggregate_id=str(RUN_ID),
        sequence=1,
        topic="run.completed",
        payload={"result": "accepted"},
        idempotency_key="outbox-key",
        lease_owner="publisher-a",
        lease_until=NOW + timedelta(seconds=30),
        lease_generation=1,
        lease_nonce=LEASE_NONCE,
        attempts=1,
        trace_carrier=TRACE,
        correlation_id="request-7f8f3d",
    )
    without_trace = _inbox()
    message = _inbox(
        trace_carrier=lease.trace_carrier,
        correlation_id=lease.correlation_id,
    )
    receipt = InboxReceipt(
        consumer=message.consumer,
        message_id=message.message_id,
        payload_hash=message.payload_hash,
        duplicate=False,
        processed_at=message.processed_at,
        result={"status": "accepted"},
        trace_carrier=message.trace_carrier,
        correlation_id=message.correlation_id,
    )

    assert without_trace.payload_hash == message.payload_hash
    assert receipt.trace_carrier == TRACE
    assert receipt.correlation_id == "request-7f8f3d"


def _enqueue(**updates: object) -> EnqueueJob:
    return EnqueueJob(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type="research",
        payload={"snapshot": "one"},
        idempotency_key="job-key",
        not_before=NOW,
        deadline_at=NOW + timedelta(minutes=5),
        max_attempts=3,
        created_at=NOW,
        **updates,
    )


def _inbox(**updates: object) -> InboxMessage:
    return InboxMessage(
        consumer="worker",
        message_id="message-1",
        payload={"result": "accepted"},
        received_at=NOW,
        processed_at=NOW,
        **updates,
    )
