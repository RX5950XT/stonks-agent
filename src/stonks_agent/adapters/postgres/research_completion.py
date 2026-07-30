"""PostgreSQL preflight for exact snapshot-scoped research leases."""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    DatasetSnapshotEvidenceRow,
    DatasetSnapshotRow,
    JobRow,
    RunDatasetSnapshotRow,
    WorkflowRunRow,
)
from stonks_agent.adapters.postgres.repositories import PostgresEvidenceRepository
from stonks_agent.domain.dataset_snapshot import CanonicalDatasetSnapshotManifest
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.job import JobLease, JobStatus
from stonks_agent.domain.research_job import (
    ResearchLeaseInput,
    SnapshotForecastContext,
)
from stonks_agent.domain.research_run import ResearchRunRequest
from stonks_agent.domain.workflow import WorkflowStatus
from stonks_contracts.common import stable_payload_hash


class PostgresResearchLeasePreflight:
    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def preflight(
        self,
        lease: JobLease,
        *,
        now: datetime,
    ) -> Result[ResearchLeaseInput]:
        if now.tzinfo is None or now.utcoffset() is None:
            return _failure(ErrorCode.INVALID_INPUT, "Research time is invalid")
        try:
            with Session(self._engine) as session, session.begin():
                database_now = session.scalar(select(func.clock_timestamp()))
                if (
                    not isinstance(database_now, datetime)
                    or database_now.tzinfo is None
                    or database_now.utcoffset() is None
                ):
                    return _failure(
                        ErrorCode.INTERNAL_ERROR,
                        "Research database time is invalid",
                    )
                return _preflight(session, lease, database_now)
        except (SQLAlchemyError, ValidationError, ValueError):
            return _failure(
                ErrorCode.INTERNAL_ERROR,
                "Research preflight failed",
            )


def _preflight(
    session: Session,
    lease: JobLease,
    now: datetime,
) -> Result[ResearchLeaseInput]:
    job = session.scalar(
        select(JobRow).where(JobRow.job_id == lease.job_id).with_for_update()
    )
    run = session.scalar(
        select(WorkflowRunRow)
        .where(WorkflowRunRow.run_id == lease.run_id)
        .with_for_update()
    )
    if not _valid_fence(job, run, lease, now):
        return _failure(ErrorCode.CONFLICT, "Research lease is stale or invalid")
    assert job is not None
    assert run is not None
    request = ResearchRunRequest.model_validate(
        {**job.payload, "requested_at": job.created_at}
    )
    link = session.get(RunDatasetSnapshotRow, lease.run_id)
    snapshot = (
        session.get(DatasetSnapshotRow, request.snapshot_id)
        if link is not None and link.snapshot_id == request.snapshot_id
        else None
    )
    if not _valid_run_and_snapshot(job, run, request, snapshot):
        return _failure(
            ErrorCode.CONFLICT,
            "Research job scope is invalid",
        )
    assert snapshot is not None
    context = _snapshot_context(snapshot)
    if isinstance(context, Failure):
        return context
    evidence_ids = tuple(
        session.scalars(
            select(DatasetSnapshotEvidenceRow.evidence_id)
            .where(DatasetSnapshotEvidenceRow.snapshot_id == request.snapshot_id)
            .order_by(DatasetSnapshotEvidenceRow.evidence_id)
        )
    )
    if not evidence_ids:
        return _failure(
            ErrorCode.DATA_UNAVAILABLE,
            "Research snapshot evidence is unavailable",
        )
    repository = PostgresEvidenceRepository(session)
    evidence = []
    for evidence_id in evidence_ids:
        loaded = repository.get(evidence_id)
        if isinstance(loaded, Failure):
            return loaded
        evidence.append(loaded.value)
    try:
        return Success(
            ResearchLeaseInput(
                request=request,
                snapshot=context.value,
                evidence=tuple(evidence),
            )
        )
    except ValidationError:
        return _failure(
            ErrorCode.CONFLICT,
            "Research snapshot evidence violates request scope",
        )


def _valid_fence(
    job: JobRow | None,
    run: WorkflowRunRow | None,
    lease: JobLease,
    now: datetime,
) -> bool:
    return (
        job is not None
        and run is not None
        and job.run_id == lease.run_id
        and job.job_type == "research_pipeline"
        and job.status == JobStatus.LEASED.value
        and job.lease_owner == lease.lease_owner
        and job.attempt_generation == lease.attempt_generation
        and job.attempt_nonce == lease.attempt_nonce
        and job.lease_until == lease.lease_until
        and job.deadline_at == lease.deadline_at
        and job.lease_until is not None
        and job.lease_until > now
        and job.deadline_at > now
        and job.payload == lease.payload
        and job.payload_hash == stable_payload_hash(lease.payload)
    )


def _valid_run_and_snapshot(
    job: JobRow,
    run: WorkflowRunRow,
    request: ResearchRunRequest,
    snapshot: DatasetSnapshotRow | None,
) -> bool:
    return (
        run.run_type == "research_report"
        and run.status
        in {
            WorkflowStatus.PENDING.value,
            WorkflowStatus.RUNNING.value,
        }
        and run.as_of == request.as_of
        and run.policy_id == request.research_profile_id
        and run.input_hash == request.input_hash
        and run.owner_subject == request.owner_subject
        and job.created_at == request.requested_at
        and job.payload == request.model_dump(mode="json", exclude={"requested_at"})
        and snapshot is not None
        and snapshot.snapshot_id == request.snapshot_id
        and snapshot.as_of == request.as_of
        and snapshot.cutoff_at <= request.as_of
        and snapshot.created_at <= request.requested_at
    )


def _snapshot_context(
    snapshot: DatasetSnapshotRow,
) -> Result[SnapshotForecastContext]:
    try:
        manifest = CanonicalDatasetSnapshotManifest.model_validate(snapshot.manifest)
        if (
            manifest.snapshot_id != snapshot.snapshot_id
            or manifest.content_hash != snapshot.content_hash
            or snapshot.manifest_artifact_hash != snapshot.content_hash
        ):
            raise ValueError("snapshot manifest identity changed")
        return Success(
            SnapshotForecastContext(
                snapshot_id=snapshot.snapshot_id,
                manifest_artifact_hash=snapshot.manifest_artifact_hash,
                content_hash=snapshot.content_hash,
                provider=manifest.provider,
                endpoint=manifest.endpoint,
            )
        )
    except (ValidationError, ValueError):
        return _failure(
            ErrorCode.CONFLICT,
            "Research snapshot manifest is invalid",
        )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
