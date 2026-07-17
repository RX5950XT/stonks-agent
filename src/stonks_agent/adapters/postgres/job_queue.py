"""PostgreSQL SKIP LOCKED queue with lease fencing and atomic completion."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta

from sqlalchemy import Engine, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from stonks_agent.adapters.observability.context import current_trace_context
from stonks_agent.adapters.postgres.durable_trace import trace_carrier_from_columns
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
from stonks_agent.domain.telemetry import ComponentName, OperationName
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_agent.ports.telemetry import OperationRecorderPort
from stonks_contracts.common import stable_payload_hash


class PostgresJobQueue:
    def __init__(
        self,
        engine: Engine,
        *,
        recorder: OperationRecorderPort | None = None,
    ) -> None:
        self._engine = engine
        self._recorder = recorder

    def enqueue(self, request: EnqueueJob) -> Result[JobRecord]:
        return self._record(OperationName.ENQUEUE, lambda: self._enqueue(request))

    def _enqueue(self, request: EnqueueJob) -> Result[JobRecord]:
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
                    traceparent=(
                        request.trace_carrier.traceparent
                        if request.trace_carrier is not None
                        else None
                    ),
                    tracestate=(
                        request.trace_carrier.tracestate
                        if request.trace_carrier is not None
                        else None
                    ),
                    correlation_id=request.correlation_id,
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
        return self._record(
            OperationName.CLAIM,
            lambda: self._claim(
                worker_id=worker_id,
                now=now,
                lease_for=lease_for,
            ),
        )

    def _claim(
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
        artifact: ArtifactManifest | None = None,
    ) -> Result[JobCompletionReceipt]:
        return self._record(
            OperationName.COMPLETE,
            lambda: self._complete(request, now=now, artifact=artifact),
        )

    def _complete(
        self,
        request: CompleteJob,
        *,
        now: datetime,
        artifact: ArtifactManifest | None = None,
    ) -> Result[JobCompletionReceipt]:
        if now.tzinfo is None or now.utcoffset() is None:
            return _failure(ErrorCode.INVALID_INPUT, "Completion time is invalid")
        try:
            return self._complete_transaction(request, artifact)
        except (IntegrityError, ValueError):
            return _failure(ErrorCode.CONFLICT, "Job completion audit conflicts")
        except SQLAlchemyError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Job completion failed")

    def _record[T](
        self,
        operation: OperationName,
        call: Callable[[], Result[T]],
    ) -> Result[T]:
        if self._recorder is None:
            return call()
        captured: list[Result[T]] = []
        raised: list[BaseException] = []
        executed = False

        def invoke() -> Result[T]:
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
            self._recorder.record_result(
                component=ComponentName.QUEUE,
                operation=operation,
                call=invoke,
                parent=current_trace_context(),
            )
        return invoke()

    def _complete_transaction(
        self,
        request: CompleteJob,
        artifact: ArtifactManifest | None,
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
            return _complete_active_job(session, row, request, artifact)


def _complete_active_job(
    session: Session,
    row: JobRow,
    request: CompleteJob,
    manifest: ArtifactManifest | None,
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
    if manifest is not None and row.job_type != "tradingagents_research":
        return _failure(
            ErrorCode.CAPABILITY_DENIED,
            "Only TradingAgents research jobs may register worker artifacts",
        )
    run = session.scalar(
        select(WorkflowRunRow)
        .where(WorkflowRunRow.run_id == row.run_id)
        .with_for_update()
    )
    if run is None:
        return _failure(ErrorCode.CONFLICT, "Owning run was not found")
    database_now = _database_now(session)
    if database_now is None:
        return _database_time_failure("Job completion")
    if lease_until <= database_now or row.deadline_at <= database_now:
        return _failure(ErrorCode.CONFLICT, "Job lease is stale or invalid")
    if manifest is not None:
        registered = _register_result_artifact(session, request, manifest, database_now)
        if isinstance(registered, Failure):
            return registered
    artifact = session.scalar(
        select(ArtifactManifestRow)
        .where(ArtifactManifestRow.content_hash == request.result_artifact_hash)
        .with_for_update()
    )
    if artifact is None:
        return _failure(ErrorCode.NOT_FOUND, "Result artifact was not finalized")
    receipt = commit_job_result(session, row, run, artifact, request, database_now)
    session.flush()
    return Success(receipt)


def _register_result_artifact(
    session: Session,
    request: CompleteJob,
    manifest: ArtifactManifest,
    database_now: datetime,
) -> Result[bool]:
    if (
        manifest.content_hash != request.result_artifact_hash
        or not 1 <= manifest.size_bytes <= 2_097_152
        or manifest.finalized_at > database_now
        or manifest.metadata.source != "tradingagents-isolated-worker"
        or manifest.metadata.media_type != "application/json"
        or manifest.metadata.license_tag != "Apache-2.0"
        or manifest.metadata.sensitivity.value != "internal"
        or dict(manifest.metadata.attributes)
        != {"schema": "tradingagents-worker-result/1.0.0"}
    ):
        return _failure(ErrorCode.CONFLICT, "Result artifact metadata is invalid")
    candidate = ArtifactManifestRow(
        content_hash=manifest.content_hash,
        size_bytes=manifest.size_bytes,
        media_type=manifest.metadata.media_type,
        license_tag=manifest.metadata.license_tag,
        sensitivity=manifest.metadata.sensitivity.value,
        source=manifest.metadata.source,
        finalized_at=manifest.finalized_at,
        storage_uri=manifest.storage_uri,
        metadata_payload=manifest.metadata.model_dump(mode="json"),
    )
    existing = session.get(ArtifactManifestRow, manifest.content_hash)
    if existing is None:
        session.add(candidate)
        session.flush()
        return Success(True)
    matches = all(
        getattr(existing, field) == getattr(candidate, field)
        for field in (
            "size_bytes",
            "media_type",
            "license_tag",
            "sensitivity",
            "source",
            "finalized_at",
            "storage_uri",
            "metadata_payload",
        )
    )
    if not matches:
        return _failure(ErrorCode.CONFLICT, "Result artifact metadata conflicts")
    return Success(True)


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
        trace_carrier=trace_carrier_from_columns(row.traceparent, row.tracestate),
        correlation_id=row.correlation_id,
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
        trace_carrier=trace_carrier_from_columns(row.traceparent, row.tracestate),
        correlation_id=row.correlation_id,
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
