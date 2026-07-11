"""Atomic PostgreSQL run and job creation for snapshot ingestion."""

from __future__ import annotations

from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
        identifiers = _identifiers(request.idempotency_key)
        run_key = f"snapshot:{request.idempotency_key}"
        payload = request.model_dump(mode="json") | {
            "snapshot_id": str(identifiers.snapshot_id)
        }
        try:
            with Session(self._engine, expire_on_commit=False) as session, session.begin():
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
                        version=1,
                        created_at=request.requested_at,
                        updated_at=request.requested_at,
                    )
                )
                session.flush()
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
                    return _failure(ErrorCode.CONFLICT, "Snapshot request already exists")
                return _existing_result(session, existing, request, identifiers)


def _existing_result(
    session: Session,
    run: WorkflowRunRow,
    request: CreateSnapshotRequest,
    identifiers: SnapshotJobRefs,
) -> Result[SnapshotJobRefs]:
    if run.input_hash != request.input_hash:
        return _failure(ErrorCode.CONFLICT, "Snapshot idempotency payload mismatch")
    job = session.scalar(select(JobRow).where(JobRow.run_id == run.run_id))
    if job is None or job.job_id != identifiers.job_id:
        return _failure(ErrorCode.CONFLICT, "Snapshot request job is missing")
    return Success(identifiers)


def _identifiers(idempotency_key: str) -> SnapshotJobRefs:
    def identifier(kind: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"stonks:snapshot:{idempotency_key}:{kind}")

    return SnapshotJobRefs(
        run_id=identifier("run"),
        job_id=identifier("job"),
        snapshot_id=identifier("snapshot"),
        evidence_refs=(),
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
