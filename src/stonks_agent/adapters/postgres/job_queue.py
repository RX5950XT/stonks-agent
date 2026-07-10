"""PostgreSQL SKIP LOCKED queue with lease fencing and atomic completion."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Engine, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    ArtifactManifestRow,
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
            with Session(self._engine, expire_on_commit=False) as session, session.begin():
                existing = session.scalar(
                    select(JobRow).where(
                        JobRow.idempotency_key == request.idempotency_key
                    )
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
            with Session(self._engine) as session:
                existing = session.scalar(
                    select(JobRow).where(
                        JobRow.idempotency_key == request.idempotency_key
                    )
                )
                if existing is None:
                    return _failure(ErrorCode.CONFLICT, "Job already exists")
                return _same_or_conflicting_job(existing, request)

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
        with Session(self._engine, expire_on_commit=False) as session, session.begin():
            self._dead_letter_unclaimable(session, now)
            row = session.scalar(
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
            if row is None:
                return _failure(ErrorCode.NOT_FOUND, "No claimable job")
            row.status = JobStatus.LEASED.value
            row.attempts += 1
            row.attempt_generation += 1
            row.attempt_nonce = nonce
            row.lease_owner = worker_id
            row.lease_until = now + lease_for
            row.updated_at = now
            session.flush()
            return Success(_job_lease(row))

    def complete(
        self,
        request: CompleteJob,
        *,
        now: datetime,
    ) -> Result[JobCompletionReceipt]:
        if now.tzinfo is None or now.utcoffset() is None:
            return _failure(ErrorCode.INVALID_INPUT, "Completion time is invalid")
        with Session(self._engine, expire_on_commit=False) as session, session.begin():
            row = session.scalar(
                select(JobRow)
                .where(JobRow.job_id == request.job_id)
                .with_for_update()
            )
            if row is None:
                return _failure(ErrorCode.NOT_FOUND, "Job was not found")
            if row.status == JobStatus.SUCCEEDED.value:
                return self._completed_receipt(session, row, request)
            invalid_lease = (
                row.status != JobStatus.LEASED.value
                or row.lease_owner != request.worker_id
                or row.attempt_generation != request.attempt_generation
                or row.attempt_nonce != request.attempt_nonce
                or row.lease_until is None
                or row.lease_until <= now
                or (row.deadline_at is not None and row.deadline_at <= now)
            )
            if invalid_lease:
                return _failure(ErrorCode.CONFLICT, "Job lease is stale or invalid")
            artifact = session.get(ArtifactManifestRow, request.result_artifact_hash)
            if artifact is None:
                return _failure(ErrorCode.NOT_FOUND, "Result artifact was not finalized")
            if row.run_id is None:
                return _failure(ErrorCode.CONFLICT, "Job has no owning run")
            run = session.scalar(
                select(WorkflowRunRow)
                .where(WorkflowRunRow.run_id == row.run_id)
                .with_for_update()
            )
            if run is None:
                return _failure(ErrorCode.CONFLICT, "Owning run was not found")
            receipt = self._commit_result(session, row, run, request, now)
            session.flush()
            return Success(receipt)

    @staticmethod
    def _dead_letter_unclaimable(session: Session, now: datetime) -> None:
        session.execute(
            update(JobRow)
            .where(
                JobRow.status.in_(
                    (JobStatus.QUEUED.value, JobStatus.LEASED.value)
                ),
                or_(
                    JobRow.deadline_at <= now,
                    (
                        (JobRow.status == JobStatus.LEASED.value)
                        & (JobRow.lease_until <= now)
                        & (JobRow.attempts >= JobRow.max_attempts)
                    ),
                ),
            )
            .values(
                status=JobStatus.DEAD_LETTER.value,
                updated_at=now,
                last_error={"code": "lease_or_deadline_exhausted"},
            )
        )

    def _commit_result(
        self,
        session: Session,
        job: JobRow,
        run: WorkflowRunRow,
        request: CompleteJob,
        now: datetime,
    ) -> JobCompletionReceipt:
        sequence = run.version + 1
        event_id = _event_id(job.job_id, request.attempt_generation)
        outbox_id = _outbox_id(job.job_id, request.attempt_generation)
        previous_hash = session.scalar(
            select(RunEventRow.event_hash)
            .where(RunEventRow.run_id == run.run_id)
            .order_by(RunEventRow.sequence.desc())
            .limit(1)
        )
        payload = {
            "job_id": str(job.job_id),
            "job_type": job.job_type,
            "result_artifact_hash": request.result_artifact_hash,
            "attempt_generation": request.attempt_generation,
        }
        event_hash = stable_payload_hash(
            {
                "event_id": str(event_id),
                "sequence": sequence,
                "previous_hash": previous_hash,
                "payload": payload,
            }
        )
        session.add(
            RunEventRow(
                event_id=event_id,
                run_id=run.run_id,
                sequence=sequence,
                event_type="job.completed",
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
                topic="job.completed",
                payload=payload,
                idempotency_key=f"job:{job.job_id}:complete:{request.attempt_generation}",
                created_at=now,
                not_before=now,
                attempts=0,
            )
        )
        run.version = sequence
        run.updated_at = now
        job.status = JobStatus.SUCCEEDED.value
        job.result_artifact_hash = request.result_artifact_hash
        job.updated_at = now
        return JobCompletionReceipt(
            job_id=job.job_id,
            run_id=run.run_id,
            event_id=event_id,
            outbox_id=outbox_id,
            sequence=sequence,
            result_artifact_hash=request.result_artifact_hash,
            completed_at=now,
        )

    def _completed_receipt(
        self,
        session: Session,
        job: JobRow,
        request: CompleteJob,
    ) -> Result[JobCompletionReceipt]:
        same_result = (
            job.lease_owner == request.worker_id
            and job.attempt_generation == request.attempt_generation
            and job.attempt_nonce == request.attempt_nonce
            and job.result_artifact_hash == request.result_artifact_hash
            and job.run_id is not None
        )
        if not same_result:
            return _failure(ErrorCode.CONFLICT, "Completed job result conflicts")
        event_id = _event_id(job.job_id, request.attempt_generation)
        event = session.get(RunEventRow, event_id)
        if event is None or job.run_id is None:
            return _failure(ErrorCode.CONFLICT, "Completed job audit event is missing")
        return Success(
            JobCompletionReceipt(
                job_id=job.job_id,
                run_id=job.run_id,
                event_id=event_id,
                outbox_id=_outbox_id(job.job_id, request.attempt_generation),
                sequence=event.sequence,
                result_artifact_hash=request.result_artifact_hash,
                completed_at=event.occurred_at,
            )
        )


def _same_or_conflicting_job(
    row: JobRow,
    request: EnqueueJob,
) -> Result[JobRecord]:
    if row.payload_hash != request.payload_hash:
        return _failure(ErrorCode.CONFLICT, "Job idempotency payload mismatch")
    return Success(_job_record(row))


def _job_record(row: JobRow) -> JobRecord:
    if row.run_id is None or row.deadline_at is None:
        raise ValueError("canonical queued job requires run_id and deadline")
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
    if (
        row.run_id is None
        or row.attempt_nonce is None
        or row.lease_owner is None
        or row.lease_until is None
        or row.deadline_at is None
    ):
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


def _event_id(job_id: UUID, generation: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"stonks:job:{job_id}:event:{generation}")


def _outbox_id(job_id: UUID, generation: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"stonks:job:{job_id}:outbox:{generation}")


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
