"""Atomic PostgreSQL run and job creation for snapshot ingestion."""

from __future__ import annotations

from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.durable_trace import current_durable_trace
from stonks_agent.adapters.postgres.models import JobRow, WorkflowRunRow
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.job import JobStatus
from stonks_agent.domain.snapshot import CreateSnapshotRequest, SnapshotJobRefs
from stonks_agent.domain.workflow import WorkflowStatus
from stonks_contracts.common import stable_payload_hash


class PostgresSnapshotRequestStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def submit(self, request: CreateSnapshotRequest) -> Result[SnapshotJobRefs]:
        owner_scope = _owner_scope(request.owner_subject)
        identifiers = _identifiers(owner_scope, request.idempotency_key)
        run_key = f"snapshot:{owner_scope}:{request.idempotency_key}"
        payload = request.model_dump(mode="json")
        try:
            with (
                Session(self._engine, expire_on_commit=False) as session,
                session.begin(),
            ):
                existing = session.scalar(
                    select(WorkflowRunRow).where(
                        WorkflowRunRow.idempotency_key == run_key
                    )
                )
                if existing is not None:
                    return _existing_result(session, existing, request, identifiers)
                session.add(
                    WorkflowRunRow(
                        run_id=identifiers.run_id,
                        run_type="data_snapshot",
                        status=WorkflowStatus.PENDING.value,
                        as_of=request.as_of,
                        policy_id=request.provider_policy_id,
                        idempotency_key=run_key,
                        input_hash=request.input_hash,
                        owner_subject=request.owner_subject,
                        version=1,
                        created_at=request.requested_at,
                        updated_at=request.requested_at,
                    )
                )
                session.flush()
                trace_carrier, correlation_id = current_durable_trace()
                session.add(
                    JobRow(
                        job_id=identifiers.job_id,
                        run_id=identifiers.run_id,
                        job_type="create_snapshot",
                        payload=payload,
                        payload_hash=stable_payload_hash(payload),
                        status=JobStatus.QUEUED.value,
                        idempotency_key=f"{run_key}:job",
                        not_before=request.requested_at,
                        deadline_at=request.requested_at + timedelta(minutes=15),
                        attempts=0,
                        max_attempts=3,
                        attempt_generation=0,
                        traceparent=(
                            trace_carrier.traceparent
                            if trace_carrier is not None
                            else None
                        ),
                        tracestate=(
                            trace_carrier.tracestate
                            if trace_carrier is not None
                            else None
                        ),
                        correlation_id=correlation_id,
                        created_at=request.requested_at,
                        updated_at=request.requested_at,
                    )
                )
                session.flush()
                return Success(identifiers)
        except IntegrityError:
            with Session(self._engine) as session:
                existing = session.scalar(
                    select(WorkflowRunRow).where(
                        WorkflowRunRow.idempotency_key == run_key
                    )
                )
                if existing is None:
                    return _failure(
                        ErrorCode.CONFLICT, "Snapshot request already exists"
                    )
                return _existing_result(session, existing, request, identifiers)


def _existing_result(
    session: Session,
    run: WorkflowRunRow,
    request: CreateSnapshotRequest,
    identifiers: SnapshotJobRefs,
) -> Result[SnapshotJobRefs]:
    run_key = (
        f"snapshot:{_owner_scope(request.owner_subject)}:{request.idempotency_key}"
    )
    if not _run_identity_matches(run, request, identifiers, run_key):
        return _identity_conflict()
    jobs = tuple(session.scalars(select(JobRow).where(JobRow.run_id == run.run_id)))
    if len(jobs) != 1 or not _job_identity_matches(
        jobs[0], request, identifiers, run_key
    ):
        return _identity_conflict()
    return Success(identifiers)


def _run_identity_matches(
    run: WorkflowRunRow,
    request: CreateSnapshotRequest,
    identifiers: SnapshotJobRefs,
    run_key: str,
) -> bool:
    return (
        run.run_id == identifiers.run_id
        and run.run_type == "data_snapshot"
        and run.as_of == request.as_of
        and run.policy_id == request.provider_policy_id
        and run.idempotency_key == run_key
        and run.input_hash == request.input_hash
        and run.owner_subject == request.owner_subject
        and run.created_at == request.requested_at
    )


def _job_identity_matches(
    job: JobRow,
    request: CreateSnapshotRequest,
    identifiers: SnapshotJobRefs,
    run_key: str,
) -> bool:
    payload = request.model_dump(mode="json")
    return (
        job.job_id == identifiers.job_id
        and job.run_id == identifiers.run_id
        and job.job_type == "create_snapshot"
        and job.payload == payload
        and job.payload_hash == stable_payload_hash(payload)
        and job.idempotency_key == f"{run_key}:job"
        and job.not_before == request.requested_at
        and job.deadline_at == request.requested_at + timedelta(minutes=15)
        and job.max_attempts == 3
        and job.created_at == request.requested_at
    )


def _identity_conflict() -> Failure:
    return _failure(
        ErrorCode.CONFLICT,
        "Snapshot idempotency immutable identity mismatch",
    )


def _identifiers(owner_scope: str, idempotency_key: str) -> SnapshotJobRefs:
    def identifier(kind: str) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"stonks:snapshot:{owner_scope}:{idempotency_key}:{kind}",
        )

    return SnapshotJobRefs(
        run_id=identifier("run"),
        job_id=identifier("job"),
        snapshot_id=None,
        evidence_refs=(),
    )


def _owner_scope(owner_subject: str) -> str:
    return stable_payload_hash({"owner_subject": owner_subject})[:32]


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
