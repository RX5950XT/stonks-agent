"""Canonical audit transitions and retry verification for generic jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import or_, select
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
    FailJob,
    JobCompletionReceipt,
    JobFailureReceipt,
    JobStatus,
)
from stonks_agent.domain.workflow import WorkflowStatus
from stonks_contracts.common import stable_payload_hash


def dead_letter_unclaimable(session: Session, now: datetime) -> None:
    """Append one fenced terminal audit transition per unclaimable job."""

    jobs = tuple(
        session.scalars(
            select(JobRow)
            .where(
                JobRow.status.in_((JobStatus.QUEUED.value, JobStatus.LEASED.value)),
                or_(
                    JobRow.deadline_at <= now,
                    (
                        (JobRow.status == JobStatus.LEASED.value)
                        & (JobRow.lease_until <= now)
                        & (JobRow.attempts >= JobRow.max_attempts)
                    ),
                ),
            )
            .order_by(JobRow.run_id, JobRow.job_id)
            .with_for_update(skip_locked=True)
            .limit(100)
        )
    )
    for job in jobs:
        _dead_letter_job(session, job, now)


def commit_job_result(
    session: Session,
    job: JobRow,
    run: WorkflowRunRow,
    artifact: ArtifactManifestRow,
    request: CompleteJob,
    now: datetime,
) -> JobCompletionReceipt:
    """Commit the immutable result and its event/outbox graph."""

    _require_valid_audit_head(session, run)
    sequence = run.version + 1
    event_id, outbox_id = _audit_ids(job.job_id, request.attempt_generation)
    previous_hash = _previous_hash(session, run.run_id)
    payload = _completion_payload(job, run, artifact, request)
    event_hash = _event_hash(event_id, sequence, previous_hash, payload)
    _add_audit_rows(
        session,
        run,
        job,
        event_id=event_id,
        outbox_id=outbox_id,
        sequence=sequence,
        event_type="job.completed",
        payload=payload,
        idempotency_key=(f"job:{job.job_id}:complete:{request.attempt_generation}"),
        occurred_at=now,
        previous_hash=previous_hash,
        event_hash=event_hash,
    )
    _mark_completed(job, run, request, sequence, now)
    return JobCompletionReceipt(
        job_id=job.job_id,
        run_id=run.run_id,
        event_id=event_id,
        outbox_id=outbox_id,
        sequence=sequence,
        result_artifact_hash=request.result_artifact_hash,
        completed_at=now,
    )


def completed_job_receipt(
    session: Session,
    job: JobRow,
    request: CompleteJob,
) -> Result[JobCompletionReceipt]:
    """Rebuild and verify the full immutable graph before prior success."""

    event_id, outbox_id = _audit_ids(job.job_id, request.attempt_generation)
    run = session.scalar(
        select(WorkflowRunRow)
        .where(WorkflowRunRow.run_id == job.run_id)
        .with_for_update()
    )
    event = session.scalar(
        select(RunEventRow).where(RunEventRow.event_id == event_id).with_for_update()
    )
    outbox = session.scalar(
        select(OutboxRow).where(OutboxRow.outbox_id == outbox_id).with_for_update()
    )
    artifact = session.scalar(
        select(ArtifactManifestRow)
        .where(ArtifactManifestRow.content_hash == request.result_artifact_hash)
        .with_for_update()
    )
    if any(value is None for value in (run, event, outbox, artifact)):
        return _conflict("Completed job canonical graph is incomplete")
    assert isinstance(run, WorkflowRunRow)
    assert isinstance(event, RunEventRow)
    assert isinstance(outbox, OutboxRow)
    assert isinstance(artifact, ArtifactManifestRow)
    if not _completed_job_is_valid(job, event, request):
        return _conflict("Completed job result conflicts")
    payload = _completion_payload(job, run, artifact, request)
    if not _completed_graph_is_valid(
        session, job, run, event, outbox, payload, request
    ):
        return _conflict("Completed job canonical graph is invalid")
    return Success(
        JobCompletionReceipt(
            job_id=job.job_id,
            run_id=job.run_id,
            event_id=event.event_id,
            outbox_id=outbox.outbox_id,
            sequence=event.sequence,
            result_artifact_hash=request.result_artifact_hash,
            completed_at=event.occurred_at,
        )
    )


def commit_job_failure(
    session: Session,
    job: JobRow,
    run: WorkflowRunRow,
    request: FailJob,
    now: datetime,
) -> JobFailureReceipt:
    """Commit one explicit fenced failure and its immutable audit graph."""

    if stable_payload_hash(job.payload) != job.payload_hash:
        raise ValueError("failed job payload is not canonical")
    if job.result_artifact_hash is not None:
        raise ValueError("failed job already has a result")
    _require_valid_audit_head(session, run)
    sequence = run.version + 1
    event_id, outbox_id = _audit_ids(job.job_id, request.attempt_generation)
    previous_hash = _previous_hash(session, run.run_id)
    payload = _failure_payload(job, run, request)
    event_hash = _event_hash(event_id, sequence, previous_hash, payload)
    _add_audit_rows(
        session,
        run,
        job,
        event_id=event_id,
        outbox_id=outbox_id,
        sequence=sequence,
        event_type="job.dead_lettered",
        payload=payload,
        idempotency_key=f"job:{job.job_id}:dead-letter:{request.attempt_generation}",
        occurred_at=now,
        previous_hash=previous_hash,
        event_hash=event_hash,
    )
    _mark_explicit_failure(job, run, request, sequence, now)
    return JobFailureReceipt(
        job_id=job.job_id,
        run_id=run.run_id,
        event_id=event_id,
        outbox_id=outbox_id,
        sequence=sequence,
        error_code=request.error_code,
        reason_code=request.reason_code,
        failed_at=now,
    )


def failed_job_receipt(
    session: Session,
    job: JobRow,
    request: FailJob,
) -> Result[JobFailureReceipt]:
    """Rebuild and verify an explicit failure before acknowledging a retry."""

    event_id, outbox_id = _audit_ids(job.job_id, request.attempt_generation)
    run = session.scalar(
        select(WorkflowRunRow)
        .where(WorkflowRunRow.run_id == job.run_id)
        .with_for_update()
    )
    event = session.scalar(
        select(RunEventRow).where(RunEventRow.event_id == event_id).with_for_update()
    )
    outbox = session.scalar(
        select(OutboxRow).where(OutboxRow.outbox_id == outbox_id).with_for_update()
    )
    if any(value is None for value in (run, event, outbox)):
        return _conflict("Failed job canonical graph is incomplete")
    assert isinstance(run, WorkflowRunRow)
    assert isinstance(event, RunEventRow)
    assert isinstance(outbox, OutboxRow)
    payload = _failure_payload(job, run, request)
    if not _failed_job_is_valid(job, event, request):
        return _conflict("Failed job command conflicts")
    if not _failed_graph_is_valid(session, job, run, event, outbox, payload, request):
        return _conflict("Failed job canonical graph is invalid")
    return Success(
        JobFailureReceipt(
            job_id=job.job_id,
            run_id=job.run_id,
            event_id=event.event_id,
            outbox_id=outbox.outbox_id,
            sequence=event.sequence,
            error_code=request.error_code,
            reason_code=request.reason_code,
            failed_at=event.occurred_at,
        )
    )


def _dead_letter_job(session: Session, job: JobRow, now: datetime) -> None:
    if stable_payload_hash(job.payload) != job.payload_hash:
        raise ValueError("unclaimable job payload is not canonical")
    if job.result_artifact_hash is not None:
        raise ValueError("unclaimable job already has a result")
    run = session.scalar(
        select(WorkflowRunRow)
        .where(WorkflowRunRow.run_id == job.run_id)
        .with_for_update()
    )
    if run is None:
        raise ValueError("unclaimable job has no owning run")
    _require_valid_audit_head(session, run)
    reason = _dead_letter_reason(job, now)
    generation = job.attempt_generation
    sequence = run.version + 1
    event_id, outbox_id = _audit_ids(job.job_id, generation)
    previous_hash = _previous_hash(session, run.run_id)
    payload = {
        "job_id": str(job.job_id),
        "job_type": job.job_type,
        "attempt_generation": generation,
        "reason": reason,
        "status": JobStatus.DEAD_LETTER.value,
    }
    event_hash = _event_hash(event_id, sequence, previous_hash, payload)
    _add_audit_rows(
        session,
        run,
        job,
        event_id=event_id,
        outbox_id=outbox_id,
        sequence=sequence,
        event_type="job.dead_lettered",
        payload=payload,
        idempotency_key=f"job:{job.job_id}:dead-letter:{generation}",
        occurred_at=now,
        previous_hash=previous_hash,
        event_hash=event_hash,
    )
    _mark_dead_letter(job, run, reason, sequence, now)


def _add_audit_rows(
    session: Session,
    run: WorkflowRunRow,
    job: JobRow,
    *,
    event_id: UUID,
    outbox_id: UUID,
    sequence: int,
    event_type: str,
    payload: dict[str, object],
    idempotency_key: str,
    occurred_at: datetime,
    previous_hash: str | None,
    event_hash: str,
) -> None:
    session.add(
        RunEventRow(
            event_id=event_id,
            run_id=run.run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            occurred_at=occurred_at,
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
            idempotency_key=idempotency_key,
            created_at=occurred_at,
            not_before=occurred_at,
            attempts=0,
            traceparent=job.traceparent,
            tracestate=job.tracestate,
            correlation_id=job.correlation_id,
        )
    )


def _mark_dead_letter(
    job: JobRow,
    run: WorkflowRunRow,
    reason: str,
    sequence: int,
    now: datetime,
) -> None:
    job.status = JobStatus.DEAD_LETTER.value
    job.attempt_nonce = None
    job.lease_owner = None
    job.lease_until = None
    job.last_error = {
        "code": reason,
        "attempt_generation": job.attempt_generation,
    }
    job.updated_at = now
    run.status = WorkflowStatus.FAILED.value
    run.version = sequence
    run.updated_at = now


def _mark_explicit_failure(
    job: JobRow,
    run: WorkflowRunRow,
    request: FailJob,
    sequence: int,
    now: datetime,
) -> None:
    job.status = JobStatus.DEAD_LETTER.value
    job.attempt_nonce = None
    job.lease_owner = None
    job.lease_until = None
    job.last_error = {
        "code": request.error_code.value,
        "reason": request.reason_code,
        "attempt_generation": request.attempt_generation,
    }
    job.updated_at = now
    run.status = WorkflowStatus.FAILED.value
    run.version = sequence
    run.updated_at = now


def _mark_completed(
    job: JobRow,
    run: WorkflowRunRow,
    request: CompleteJob,
    sequence: int,
    now: datetime,
) -> None:
    run.version = sequence
    run.updated_at = now
    if job.job_type in {"research_pipeline", "tradingagents_research"}:
        run.status = WorkflowStatus.SUCCEEDED.value
    job.status = JobStatus.SUCCEEDED.value
    job.result_artifact_hash = request.result_artifact_hash
    job.updated_at = now


def _completed_job_is_valid(
    job: JobRow,
    event: RunEventRow,
    request: CompleteJob,
) -> bool:
    return (
        job.status == JobStatus.SUCCEEDED.value
        and job.lease_owner == request.worker_id
        and job.attempt_generation == request.attempt_generation
        and job.attempts == request.attempt_generation
        and job.attempt_nonce == request.attempt_nonce
        and job.result_artifact_hash == request.result_artifact_hash
        and stable_payload_hash(job.payload) == job.payload_hash
        and job.updated_at == event.occurred_at
        and job.last_error is None
    )


def _completed_graph_is_valid(
    session: Session,
    job: JobRow,
    run: WorkflowRunRow,
    event: RunEventRow,
    outbox: OutboxRow,
    payload: dict[str, object],
    request: CompleteJob,
) -> bool:
    expected_key = f"job:{job.job_id}:complete:{request.attempt_generation}"
    return (
        run.version >= event.sequence
        and _completed_run_status_is_valid(job, run)
        and _event_chain_is_valid(session, run, event.event_id)
        and event.event_type == "job.completed"
        and event.payload == payload
        and outbox.aggregate_type == "run"
        and outbox.aggregate_id == str(run.run_id)
        and outbox.sequence == event.sequence
        and outbox.topic == "job.completed"
        and outbox.payload == payload
        and outbox.idempotency_key == expected_key
        and outbox.created_at == event.occurred_at
        and outbox.not_before == event.occurred_at
        and outbox.traceparent == job.traceparent
        and outbox.tracestate == job.tracestate
        and outbox.correlation_id == job.correlation_id
    )


def _completed_run_status_is_valid(
    job: JobRow,
    run: WorkflowRunRow,
) -> bool:
    if job.job_type in {"research_pipeline", "tradingagents_research"}:
        return run.status == WorkflowStatus.SUCCEEDED.value
    return True


def _failed_job_is_valid(
    job: JobRow,
    event: RunEventRow,
    request: FailJob,
) -> bool:
    return (
        job.status == JobStatus.DEAD_LETTER.value
        and job.lease_owner is None
        and job.attempt_nonce is None
        and job.lease_until is None
        and job.attempt_generation == request.attempt_generation
        and job.attempts == request.attempt_generation
        and job.result_artifact_hash is None
        and stable_payload_hash(job.payload) == job.payload_hash
        and job.updated_at == event.occurred_at
        and job.last_error
        == {
            "code": request.error_code.value,
            "reason": request.reason_code,
            "attempt_generation": request.attempt_generation,
        }
    )


def _failed_graph_is_valid(
    session: Session,
    job: JobRow,
    run: WorkflowRunRow,
    event: RunEventRow,
    outbox: OutboxRow,
    payload: dict[str, object],
    request: FailJob,
) -> bool:
    expected_key = f"job:{job.job_id}:dead-letter:{request.attempt_generation}"
    return (
        run.status == WorkflowStatus.FAILED.value
        and run.version >= event.sequence
        and _event_chain_is_valid(session, run, event.event_id)
        and event.event_type == "job.dead_lettered"
        and event.payload == payload
        and outbox.aggregate_type == "run"
        and outbox.aggregate_id == str(run.run_id)
        and outbox.sequence == event.sequence
        and outbox.topic == "job.dead_lettered"
        and outbox.payload == payload
        and outbox.idempotency_key == expected_key
        and outbox.created_at == event.occurred_at
        and outbox.not_before == event.occurred_at
        and outbox.traceparent == job.traceparent
        and outbox.tracestate == job.tracestate
        and outbox.correlation_id == job.correlation_id
    )


def _event_chain_is_valid(
    session: Session,
    run: WorkflowRunRow,
    required_event_id: UUID,
) -> bool:
    events = tuple(
        session.scalars(
            select(RunEventRow)
            .where(RunEventRow.run_id == run.run_id)
            .order_by(RunEventRow.sequence)
        )
    )
    previous_hash: str | None = None
    expected_sequence = 2
    required_event_found = False
    for event in events:
        if event.sequence != expected_sequence or event.previous_hash != previous_hash:
            return False
        if event.event_hash != _event_hash(
            event.event_id, event.sequence, previous_hash, event.payload
        ):
            return False
        required_event_found |= event.event_id == required_event_id
        previous_hash = event.event_hash
        expected_sequence += 1
    return (
        bool(events)
        and required_event_found
        and events[-1].sequence == run.version
        and events[-1].occurred_at == run.updated_at
    )


def _require_valid_audit_head(session: Session, run: WorkflowRunRow) -> None:
    events = tuple(
        session.scalars(
            select(RunEventRow)
            .where(RunEventRow.run_id == run.run_id)
            .order_by(RunEventRow.sequence)
        )
    )
    if not events:
        if run.version != 1:
            raise ValueError("run audit head is invalid")
        return
    if run.version != events[-1].sequence:
        raise ValueError("run audit head is invalid")
    if not _event_chain_is_valid(session, run, events[-1].event_id):
        raise ValueError("run audit chain is invalid")


def _completion_payload(
    job: JobRow,
    run: WorkflowRunRow,
    artifact: ArtifactManifestRow,
    request: CompleteJob,
) -> dict[str, object]:
    return {
        "job_id": str(job.job_id),
        "job_type": job.job_type,
        "result_artifact_hash": request.result_artifact_hash,
        "attempt_generation": request.attempt_generation,
        "job_identity_hash": _job_identity_hash(job),
        "run_identity_hash": _run_identity_hash(run),
        "result_artifact_identity_hash": _artifact_identity_hash(artifact),
    }


def _failure_payload(
    job: JobRow,
    run: WorkflowRunRow,
    request: FailJob,
) -> dict[str, object]:
    return {
        "job_id": str(job.job_id),
        "job_type": job.job_type,
        "attempt_generation": request.attempt_generation,
        "worker_id": request.worker_id,
        "attempt_nonce_hash": stable_payload_hash(
            {"attempt_nonce": request.attempt_nonce}
        ),
        "error_code": request.error_code.value,
        "reason": request.reason_code,
        "status": JobStatus.DEAD_LETTER.value,
        "job_identity_hash": _job_identity_hash(job),
        "run_identity_hash": _run_identity_hash(run),
    }


def _job_identity_hash(job: JobRow) -> str:
    return stable_payload_hash(
        {
            "job_id": str(job.job_id),
            "run_id": str(job.run_id),
            "job_type": job.job_type,
            "payload": job.payload,
            "payload_hash": job.payload_hash,
            "idempotency_key": job.idempotency_key,
            "not_before": _timestamp(job.not_before),
            "deadline_at": _timestamp(job.deadline_at),
            "max_attempts": job.max_attempts,
            "created_at": _timestamp(job.created_at),
        }
    )


def _run_identity_hash(run: WorkflowRunRow) -> str:
    return stable_payload_hash(
        {
            "run_id": str(run.run_id),
            "run_type": run.run_type,
            "as_of": _timestamp(run.as_of),
            "policy_id": run.policy_id,
            "idempotency_key": run.idempotency_key,
            "input_hash": run.input_hash,
            "owner_subject": run.owner_subject,
            "created_at": _timestamp(run.created_at),
        }
    )


def _artifact_identity_hash(artifact: ArtifactManifestRow) -> str:
    return stable_payload_hash(
        {
            "content_hash": artifact.content_hash,
            "size_bytes": artifact.size_bytes,
            "media_type": artifact.media_type,
            "license_tag": artifact.license_tag,
            "sensitivity": artifact.sensitivity,
            "source": artifact.source,
            "finalized_at": _timestamp(artifact.finalized_at),
            "storage_uri": artifact.storage_uri,
            "metadata": artifact.metadata_payload,
        }
    )


def _dead_letter_reason(job: JobRow, now: datetime) -> str:
    if job.deadline_at <= now:
        return "deadline_exceeded"
    if (
        job.status == JobStatus.LEASED.value
        and job.lease_until is not None
        and job.lease_until <= now
        and job.attempts >= job.max_attempts
    ):
        return "attempts_exhausted"
    raise ValueError("job is still claimable")


def _previous_hash(session: Session, run_id: UUID) -> str | None:
    return session.scalar(
        select(RunEventRow.event_hash)
        .where(RunEventRow.run_id == run_id)
        .order_by(RunEventRow.sequence.desc())
        .limit(1)
    )


def _event_hash(
    event_id: UUID,
    sequence: int,
    previous_hash: str | None,
    payload: dict[str, object],
) -> str:
    return stable_payload_hash(
        {
            "event_id": str(event_id),
            "sequence": sequence,
            "previous_hash": previous_hash,
            "payload": payload,
        }
    )


def _audit_ids(job_id: UUID, generation: int) -> tuple[UUID, UUID]:
    prefix = f"stonks:job:{job_id}"
    return (
        uuid5(NAMESPACE_URL, f"{prefix}:event:{generation}"),
        uuid5(NAMESPACE_URL, f"{prefix}:outbox:{generation}"),
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _conflict(message: str) -> Failure:
    return Failure(StructuredError(code=ErrorCode.CONFLICT, message=message))
