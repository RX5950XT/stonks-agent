"""PostgreSQL SKIP LOCKED queue with lease fencing and atomic completion."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy import Engine, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.job_queue_audit import (
    commit_job_result,
    completed_job_receipt,
    dead_letter_unclaimable,
)
from stonks_agent.adapters.postgres.models import (
    ArtifactManifestRow,
    JobRow,
    WorkflowRunRow,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.job import (
    CompleteJob,
    EnqueueJob,
    JobCompletionReceipt,
    JobLease,
    JobRecord,
    JobStatus,
)
from stonks_contracts.common import stable_payload_hash


class PostgresJobQueue:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def enqueue(self, request: EnqueueJob) -> Result[JobRecord]:
        try:
            with (
                Session(self._engine, expire_on_commit=False) as session,
                session.begin(),
            ):
                existing = session.scalar(
                    select(JobRow)
                    .where(JobRow.idempotency_key == request.idempotency_key)
                    .with_for_update()
                )
                if existing is not None:
                    return _same_or_conflicting_job(existing, request)
                row = JobRow(
                    job_id=request.job_id,
                    run_id=request.run_id,
                    job_type=request.job_type,
                    payload=request.payload,
                    payload_hash=request.payload_hash,
                    status=JobStatus.QUEUED.value,
                    idempotency_key=request.idempotency_key,
                    not_before=request.not_before,
                    deadline_at=request.deadline_at,
                    attempts=0,
                    max_attempts=request.max_attempts,
                    attempt_generation=0,
                    created_at=request.created_at,
                    updated_at=request.created_at,
                )
                session.add(row)
                session.flush()
                return Success(_job_record(row))
        except IntegrityError:
            return _existing_after_enqueue_race(self._engine, request)
        except SQLAlchemyError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Job enqueue failed")

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> Result[JobLease]:
        if not worker_id.strip() or len(worker_id) > 128:
            return _failure(ErrorCode.INVALID_INPUT, "Worker ID is invalid")
        if now.tzinfo is None or now.utcoffset() is None or lease_for <= timedelta(0):
            return _failure(ErrorCode.INVALID_INPUT, "Lease timing is invalid")
        nonce = secrets.token_urlsafe(32)
        try:
            with (
                Session(self._engine, expire_on_commit=False) as session,
                session.begin(),
            ):
                database_now = _database_now(session)
                if database_now is None:
                    return _failure(
                        ErrorCode.INTERNAL_ERROR,
                        "Job queue database time is invalid",
                    )
                dead_letter_unclaimable(session, database_now)
                row = _next_claimable_job(session, database_now)
                if row is None:
                    return _failure(ErrorCode.NOT_FOUND, "No claimable job")
                _mark_leased(row, worker_id, nonce, database_now, lease_for)
                session.flush()
                return Success(_job_lease(row))
        except (IntegrityError, ValueError):
            return _failure(ErrorCode.CONFLICT, "Job queue audit conflicts")
        except SQLAlchemyError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Job claim failed")

    def complete(
        self,
        request: CompleteJob,
        *,
        now: datetime,
    ) -> Result[JobCompletionReceipt]:
        if now.tzinfo is None or now.utcoffset() is None:
            return _failure(ErrorCode.INVALID_INPUT, "Completion time is invalid")
        try:
            return self._complete_transaction(request)
        except (IntegrityError, ValueError):
            return _failure(ErrorCode.CONFLICT, "Job completion audit conflicts")
        except SQLAlchemyError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Job completion failed")

    def _complete_transaction(
        self,
        request: CompleteJob,
    ) -> Result[JobCompletionReceipt]:
        with Session(self._engine, expire_on_commit=False) as session, session.begin():
            row = session.scalar(
                select(JobRow).where(JobRow.job_id == request.job_id).with_for_update()
            )
            if row is None:
                return _failure(ErrorCode.NOT_FOUND, "Job was not found")
            if row.job_type == "create_snapshot":
                return _failure(
                    ErrorCode.CAPABILITY_DENIED,
                    "Snapshot jobs require canonical snapshot completion",
                )
            if row.status == JobStatus.SUCCEEDED.value:
                if _database_now(session) is None:
                    return _database_time_failure("Job completion")
                return completed_job_receipt(session, row, request)
            return _complete_active_job(session, row, request)


def _complete_active_job(
    session: Session,
    row: JobRow,
    request: CompleteJob,
) -> Result[JobCompletionReceipt]:
    lease_until = row.lease_until
    invalid_lease = (
        lease_until is None
        or row.status != JobStatus.LEASED.value
        or row.lease_owner != request.worker_id
        or row.attempt_generation != request.attempt_generation
        or row.attempt_nonce != request.attempt_nonce
    )
    if invalid_lease or lease_until is None:
        return _failure(ErrorCode.CONFLICT, "Job lease is stale or invalid")
    run = session.scalar(
        select(WorkflowRunRow)
        .where(WorkflowRunRow.run_id == row.run_id)
        .with_for_update()
    )
    if run is None:
        return _failure(ErrorCode.CONFLICT, "Owning run was not found")
    artifact = session.scalar(
        select(ArtifactManifestRow)
        .where(ArtifactManifestRow.content_hash == request.result_artifact_hash)
        .with_for_update()
    )
    if artifact is None:
        return _failure(ErrorCode.NOT_FOUND, "Result artifact was not finalized")
    database_now = _database_now(session)
    if database_now is None:
        return _database_time_failure("Job completion")
    if lease_until <= database_now or row.deadline_at <= database_now:
        return _failure(ErrorCode.CONFLICT, "Job lease is stale or invalid")
    receipt = commit_job_result(session, row, run, artifact, request, database_now)
    session.flush()
    return Success(receipt)


def _same_or_conflicting_job(
    row: JobRow,
    request: EnqueueJob,
) -> Result[JobRecord]:
    same_command = (
        row.job_id == request.job_id
        and row.run_id == request.run_id
        and row.job_type == request.job_type
        and row.payload == request.payload
        and row.payload_hash == stable_payload_hash(row.payload)
        and row.payload_hash == request.payload_hash
        and row.idempotency_key == request.idempotency_key
        and row.not_before == request.not_before
        and row.deadline_at == request.deadline_at
        and row.max_attempts == request.max_attempts
        and row.created_at == request.created_at
    )
    if not same_command:
        return _failure(ErrorCode.CONFLICT, "Job idempotency command mismatch")
    return Success(_job_record(row))


def _existing_after_enqueue_race(
    engine: Engine,
    request: EnqueueJob,
) -> Result[JobRecord]:
    try:
        with Session(engine) as session, session.begin():
            existing = session.scalar(
                select(JobRow)
                .where(JobRow.idempotency_key == request.idempotency_key)
                .with_for_update()
            )
            if existing is None:
                return _failure(ErrorCode.CONFLICT, "Job already exists")
            return _same_or_conflicting_job(existing, request)
    except SQLAlchemyError:
        return _failure(ErrorCode.INTERNAL_ERROR, "Job enqueue retry failed")


def _next_claimable_job(session: Session, now: datetime) -> JobRow | None:
    return session.scalar(
        select(JobRow)
        .where(
            or_(
                JobRow.status == JobStatus.QUEUED.value,
                (
                    (JobRow.status == JobStatus.LEASED.value)
                    & (JobRow.lease_until <= now)
                ),
            ),
            JobRow.not_before <= now,
            JobRow.deadline_at > now,
            JobRow.attempts < JobRow.max_attempts,
        )
        .order_by(JobRow.not_before, JobRow.created_at, JobRow.job_id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )


def _mark_leased(
    row: JobRow,
    worker_id: str,
    nonce: str,
    now: datetime,
    lease_for: timedelta,
) -> None:
    row.status = JobStatus.LEASED.value
    row.attempts += 1
    row.attempt_generation += 1
    row.attempt_nonce = nonce
    row.lease_owner = worker_id
    row.lease_until = now + lease_for
    row.updated_at = now


def _database_now(session: Session) -> datetime | None:
    value = session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value


def _database_time_failure(operation: str) -> Failure:
    return _failure(
        ErrorCode.INTERNAL_ERROR,
        f"{operation} database time is invalid",
    )


def _job_record(row: JobRow) -> JobRecord:
    return JobRecord(
        job_id=row.job_id,
        run_id=row.run_id,
        job_type=row.job_type,
        payload=row.payload,
        payload_hash=row.payload_hash,
        status=JobStatus(row.status),
        idempotency_key=row.idempotency_key,
        not_before=row.not_before,
        deadline_at=row.deadline_at,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        attempt_generation=row.attempt_generation,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _job_lease(row: JobRow) -> JobLease:
    if row.attempt_nonce is None or row.lease_owner is None or row.lease_until is None:
        raise ValueError("claimed job is missing lease fields")
    return JobLease(
        job_id=row.job_id,
        run_id=row.run_id,
        job_type=row.job_type,
        payload=row.payload,
        attempt_generation=row.attempt_generation,
        attempt_nonce=row.attempt_nonce,
        lease_owner=row.lease_owner,
        lease_until=row.lease_until,
        attempts=row.attempts,
        deadline_at=row.deadline_at,
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
