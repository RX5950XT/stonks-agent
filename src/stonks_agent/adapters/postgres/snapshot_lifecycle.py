"""Database-owned snapshot lease preflight and fenced failure transitions."""

from __future__ import annotations

from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    JobRow,
    OutboxRow,
    RunEventRow,
    WorkflowRunRow,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.job import JobLease, JobStatus
from stonks_agent.domain.provider_policy import ProviderPolicy
from stonks_agent.domain.snapshot import (
    CreateSnapshotRequest,
    FailSnapshotJob,
    SnapshotAttemptFailureReceipt,
    reconciliation_trace_is_authorized,
    snapshot_request_is_authorized,
)
from stonks_agent.domain.workflow import WorkflowStatus
from stonks_contracts.common import stable_payload_hash


def preflight_snapshot_lease(
    engine: Engine,
    lease: JobLease,
    *,
    now: datetime,
    policy: ProviderPolicy,
) -> Result[CreateSnapshotRequest]:
    """Read authoritative DB state before any provider or artifact I/O."""

    if not _aware(now):
        return _failure(ErrorCode.INVALID_INPUT, "Preflight time is invalid")
    try:
        with Session(engine) as session, session.begin():
            job = _locked_job(session, lease.job_id)
            if job is None:
                return _failure(ErrorCode.NOT_FOUND, "Job was not found")
            database_now = session.scalar(select(func.clock_timestamp()))
            if not isinstance(database_now, datetime) or not _aware(database_now):
                return _failure(
                    ErrorCode.INTERNAL_ERROR,
                    "Snapshot preflight database time is invalid",
                )
            fenced = _validate_preflight_fence(job, lease, database_now)
            if isinstance(fenced, Failure):
                return fenced
            run = _locked_run(session, job)
            if isinstance(run, Failure):
                return run
            return _active_request_context(job, run.value, policy)
    except SQLAlchemyError:
        return _failure(ErrorCode.INTERNAL_ERROR, "Snapshot preflight failed")


def record_snapshot_failure(
    engine: Engine,
    request: FailSnapshotJob,
    *,
    now: datetime,
    policy: ProviderPolicy,
) -> Result[SnapshotAttemptFailureReceipt]:
    """Atomically release an active fence and append safe failure audit."""

    if not _aware(now):
        return _failure(ErrorCode.INVALID_INPUT, "Failure time is invalid")
    try:
        with Session(engine, expire_on_commit=False) as session, session.begin():
            job = _locked_job(session, request.job_id)
            if job is None:
                return _failure(ErrorCode.NOT_FOUND, "Job was not found")
            database_now = session.scalar(select(func.clock_timestamp()))
            if not isinstance(database_now, datetime) or not _aware(database_now):
                return _failure(
                    ErrorCode.INTERNAL_ERROR,
                    "Snapshot failure database time is invalid",
                )
            fenced = _validate_failure_fence(job, request, database_now)
            if isinstance(fenced, Failure):
                return fenced
            run = _locked_run(session, job)
            if isinstance(run, Failure):
                return run
            context = _active_request_context(job, run.value, policy)
            if isinstance(context, Failure):
                return context
            if (
                request.reconciliation_trace is not None
                and not reconciliation_trace_is_authorized(
                    context.value, request.reconciliation_trace, policy
                )
            ):
                return _failure(
                    ErrorCode.CONFLICT,
                    "Snapshot failure reconciliation trace is unauthorized",
                )
            receipt = _append_failure(session, job, run.value, request, database_now)
            session.flush()
            return Success(receipt)
    except (IntegrityError, ValueError):
        return _failure(ErrorCode.CONFLICT, "Snapshot failure audit conflicts")
    except SQLAlchemyError:
        return _failure(ErrorCode.INTERNAL_ERROR, "Snapshot failure audit failed")


def _validate_preflight_fence(
    job: JobRow,
    lease: JobLease,
    now: datetime,
) -> Result[bool]:
    invalid = (
        job.status != JobStatus.LEASED.value
        or job.job_type != "create_snapshot"
        or job.run_id != lease.run_id
        or job.payload != lease.payload
        or stable_payload_hash(job.payload) != job.payload_hash
        or stable_payload_hash(lease.payload) != job.payload_hash
        or job.lease_owner != lease.lease_owner
        or job.attempt_generation != lease.attempt_generation
        or job.attempt_nonce != lease.attempt_nonce
        or job.attempts != lease.attempts
        or job.lease_until != lease.lease_until
        or job.lease_until is None
        or job.lease_until <= now
        or job.deadline_at != lease.deadline_at
        or job.deadline_at <= now
        or job.result_artifact_hash is not None
    )
    return _fence_result(invalid)


def _validate_failure_fence(
    job: JobRow,
    request: FailSnapshotJob,
    now: datetime,
) -> Result[bool]:
    invalid = (
        job.status != JobStatus.LEASED.value
        or job.job_type != "create_snapshot"
        or job.run_id != request.run_id
        or stable_payload_hash(job.payload) != job.payload_hash
        or job.payload_hash != request.payload_hash
        or job.lease_owner != request.worker_id
        or job.attempt_generation != request.attempt_generation
        or job.attempt_nonce != request.attempt_nonce
        or job.lease_until != request.lease_until
        or job.lease_until is None
        or job.lease_until <= now
        or job.deadline_at != request.deadline_at
        or job.deadline_at <= now
        or job.result_artifact_hash is not None
    )
    return _fence_result(invalid)


def _active_request_context(
    job: JobRow,
    run: WorkflowRunRow,
    policy: ProviderPolicy,
) -> Result[CreateSnapshotRequest]:
    try:
        request = CreateSnapshotRequest.model_validate(job.payload)
    except ValidationError:
        return _failure(ErrorCode.CONFLICT, "Snapshot job payload is invalid")
    valid = (
        snapshot_request_is_authorized(request, policy)
        and run.run_id == job.run_id
        and run.run_type == "data_snapshot"
        and run.status in {WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value}
        and run.input_hash == request.input_hash
        and run.as_of == request.as_of
        and run.policy_id == request.provider_policy_id
    )
    if not valid:
        return _failure(ErrorCode.CONFLICT, "Snapshot run authority is invalid")
    return Success(request)


def _append_failure(
    session: Session,
    job: JobRow,
    run: WorkflowRunRow,
    request: FailSnapshotJob,
    now: datetime,
) -> SnapshotAttemptFailureReceipt:
    retry = job.attempts < job.max_attempts
    status = JobStatus.QUEUED if retry else JobStatus.DEAD_LETTER
    event_type = "snapshot.retry_scheduled" if retry else "snapshot.failed"
    sequence = run.version + 1
    event_id, outbox_id = _audit_ids(job.job_id, request.attempt_generation)
    previous_hash = _previous_hash(session, run.run_id)
    payload = _failure_payload(job, request, status)
    event_hash = stable_payload_hash(
        {
            "event_id": str(event_id),
            "sequence": sequence,
            "previous_hash": previous_hash,
            "payload": payload,
        }
    )
    _add_failure_audit(
        session,
        run,
        job,
        event_id,
        outbox_id,
        sequence,
        event_type,
        payload,
        previous_hash,
        event_hash,
        request.attempt_generation,
        now,
    )
    _transition_failed_attempt(job, run, status, request, sequence, now)
    return SnapshotAttemptFailureReceipt(
        job_id=job.job_id,
        run_id=run.run_id,
        event_id=event_id,
        outbox_id=outbox_id,
        sequence=sequence,
        status=status,
        recorded_at=now,
    )


def _add_failure_audit(
    session: Session,
    run: WorkflowRunRow,
    job: JobRow,
    event_id: UUID,
    outbox_id: UUID,
    sequence: int,
    event_type: str,
    payload: dict[str, object],
    previous_hash: str | None,
    event_hash: str,
    generation: int,
    now: datetime,
) -> None:
    session.add(
        RunEventRow(
            event_id=event_id,
            run_id=run.run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            occurred_at=now,
            previous_hash=previous_hash,
            event_hash=event_hash,
        )
    )
    session.add(
        OutboxRow(
            outbox_id=outbox_id,
            aggregate_type="run",
            aggregate_id=str(run.run_id),
            sequence=sequence,
            topic=event_type,
            payload=payload,
            idempotency_key=f"job:{payload['job_id']}:failure:{generation}",
            created_at=now,
            not_before=now,
            attempts=0,
            traceparent=job.traceparent,
            tracestate=job.tracestate,
            correlation_id=job.correlation_id,
        )
    )


def _transition_failed_attempt(
    job: JobRow,
    run: WorkflowRunRow,
    status: JobStatus,
    request: FailSnapshotJob,
    sequence: int,
    now: datetime,
) -> None:
    job.status = status.value
    job.not_before = now
    job.attempt_nonce = None
    job.lease_owner = None
    job.lease_until = None
    last_error: dict[str, object] = {
        "code": request.error_code.value,
        "stage": request.stage.value,
        "attempt_generation": request.attempt_generation,
    }
    if request.reconciliation_trace_hash is not None:
        last_error["reconciliation_trace_hash"] = request.reconciliation_trace_hash
    job.last_error = last_error
    job.updated_at = now
    run.status = (
        WorkflowStatus.RUNNING.value
        if status is JobStatus.QUEUED
        else WorkflowStatus.FAILED.value
    )
    run.version = sequence
    run.updated_at = now


def _failure_payload(
    job: JobRow,
    request: FailSnapshotJob,
    status: JobStatus,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": str(job.job_id),
        "job_type": job.job_type,
        "attempt_generation": request.attempt_generation,
        "stage": request.stage.value,
        "error_code": request.error_code.value,
        "status": status.value,
    }
    if request.reconciliation_trace is not None:
        payload["reconciliation_trace"] = request.reconciliation_trace.model_dump(
            mode="json"
        )
        payload["reconciliation_trace_hash"] = request.reconciliation_trace_hash
    return payload


def _locked_job(session: Session, job_id: UUID) -> JobRow | None:
    return session.scalar(
        select(JobRow).where(JobRow.job_id == job_id).with_for_update()
    )


def _locked_run(session: Session, job: JobRow) -> Result[WorkflowRunRow]:
    run = session.scalar(
        select(WorkflowRunRow)
        .where(WorkflowRunRow.run_id == job.run_id)
        .with_for_update()
    )
    if run is None:
        return _failure(ErrorCode.CONFLICT, "Owning run was not found")
    return Success(run)


def _previous_hash(session: Session, run_id: UUID) -> str | None:
    return session.scalar(
        select(RunEventRow.event_hash)
        .where(RunEventRow.run_id == run_id)
        .order_by(RunEventRow.sequence.desc())
        .limit(1)
    )


def _audit_ids(job_id: UUID, generation: int) -> tuple[UUID, UUID]:
    prefix = f"stonks:job:{job_id}"
    return (
        uuid5(NAMESPACE_URL, f"{prefix}:event:{generation}"),
        uuid5(NAMESPACE_URL, f"{prefix}:outbox:{generation}"),
    )


def _fence_result(invalid: bool) -> Result[bool]:
    if invalid:
        return _failure(ErrorCode.CONFLICT, "Job lease is stale or invalid")
    return Success(True)


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
