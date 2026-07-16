"""Atomic research run submission and verified canonical event reads."""

from __future__ import annotations

from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    DatasetSnapshotRow,
    JobRow,
    RunDatasetSnapshotRow,
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
from stonks_agent.domain.job import JobStatus
from stonks_agent.domain.research_run import (
    CanonicalRunEvent,
    ResearchRunRefs,
    ResearchRunRequest,
)
from stonks_agent.domain.workflow import WorkflowStatus
from stonks_contracts.common import stable_payload_hash


class PostgresResearchRequestStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def submit(self, request: ResearchRunRequest) -> Result[ResearchRunRefs]:
        owner_scope = _owner_scope(request.owner_subject)
        references = _identifiers(owner_scope, request.idempotency_key)
        run_key = f"research:{owner_scope}:{request.idempotency_key}"
        try:
            with (
                Session(self._engine, expire_on_commit=False) as session,
                session.begin(),
            ):
                existing = session.scalar(
                    select(WorkflowRunRow)
                    .where(WorkflowRunRow.idempotency_key == run_key)
                    .with_for_update()
                )
                if existing is not None:
                    return _existing(session, existing, request, references, run_key)
                snapshot = session.scalar(
                    select(DatasetSnapshotRow)
                    .where(DatasetSnapshotRow.snapshot_id == request.snapshot_id)
                    .with_for_update()
                )
                validated = _validate_snapshot(snapshot, request)
                if isinstance(validated, Failure):
                    return validated
                session.add(_run_row(request, references.run_id, run_key))
                session.flush()
                session.add(_job_row(request, references, run_key))
                session.add(
                    RunDatasetSnapshotRow(
                        run_id=references.run_id,
                        snapshot_id=request.snapshot_id,
                        created_at=request.requested_at,
                    )
                )
                session.flush()
                return Success(references)
        except IntegrityError:
            return self._after_race(request, references, run_key)
        except SQLAlchemyError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Research request failed")

    def snapshot_owner(self, snapshot_id: UUID) -> Result[str]:
        try:
            with Session(self._engine) as session:
                owners = tuple(
                    session.scalars(
                        select(WorkflowRunRow.owner_subject)
                        .join(
                            RunDatasetSnapshotRow,
                            RunDatasetSnapshotRow.run_id == WorkflowRunRow.run_id,
                        )
                        .where(
                            RunDatasetSnapshotRow.snapshot_id == snapshot_id,
                            WorkflowRunRow.run_type == "data_snapshot",
                        )
                        .distinct()
                    )
                )
                if len(owners) != 1:
                    return _failure(
                        ErrorCode.NOT_FOUND,
                        "Dataset snapshot ownership was not found",
                    )
                return Success(owners[0])
        except SQLAlchemyError:
            return _failure(
                ErrorCode.INTERNAL_ERROR, "Snapshot ownership is unavailable"
            )

    def _after_race(
        self,
        request: ResearchRunRequest,
        references: ResearchRunRefs,
        run_key: str,
    ) -> Result[ResearchRunRefs]:
        try:
            with Session(self._engine) as session:
                existing = session.scalar(
                    select(WorkflowRunRow).where(
                        WorkflowRunRow.idempotency_key == run_key
                    )
                )
                if existing is None:
                    return _identity_conflict()
                return _existing(session, existing, request, references, run_key)
        except SQLAlchemyError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Research request failed")


class PostgresRunEventReader:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list_after(
        self, run_id: UUID, *, after_sequence: int, limit: int
    ) -> Result[tuple[CanonicalRunEvent, ...]]:
        if after_sequence < 0 or not 1 <= limit <= 500:
            return _failure(ErrorCode.INVALID_INPUT, "Event query is invalid")
        try:
            with Session(self._engine) as session:
                run = session.get(WorkflowRunRow, run_id)
                if run is None:
                    return _failure(ErrorCode.NOT_FOUND, "Research run was not found")
                rows = tuple(
                    session.scalars(
                        select(RunEventRow)
                        .where(RunEventRow.run_id == run_id)
                        .order_by(RunEventRow.sequence)
                    )
                )
                if len(rows) > 10_000 or not _valid_event_chain(run, rows):
                    return _failure(ErrorCode.CONFLICT, "Run event chain is invalid")
                selected = tuple(
                    _projection(row) for row in rows if row.sequence > after_sequence
                )[:limit]
                return Success(selected)
        except (SQLAlchemyError, ValueError):
            return _failure(ErrorCode.INTERNAL_ERROR, "Run events are unavailable")

    def owner_subject(self, run_id: UUID) -> Result[str]:
        try:
            with Session(self._engine) as session:
                owner = session.scalar(
                    select(WorkflowRunRow.owner_subject).where(
                        WorkflowRunRow.run_id == run_id
                    )
                )
                if owner is None:
                    return _failure(ErrorCode.NOT_FOUND, "Research run was not found")
                return Success(owner)
        except SQLAlchemyError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Run ownership is unavailable")


def _validate_snapshot(
    snapshot: DatasetSnapshotRow | None, request: ResearchRunRequest
) -> Success[bool] | Failure:
    if snapshot is None:
        return _failure(ErrorCode.NOT_FOUND, "Dataset snapshot was not found")
    if (
        snapshot.as_of != request.as_of
        or snapshot.cutoff_at > request.as_of
        or snapshot.created_at > request.requested_at
    ):
        return _failure(
            ErrorCode.CONFLICT, "Dataset snapshot is not point-in-time valid"
        )
    return Success(True)


def _run_row(request: ResearchRunRequest, run_id: UUID, run_key: str) -> WorkflowRunRow:
    return WorkflowRunRow(
        run_id=run_id,
        run_type="research_report",
        status=WorkflowStatus.PENDING.value,
        as_of=request.as_of,
        policy_id=request.research_profile_id,
        idempotency_key=run_key,
        input_hash=request.input_hash,
        owner_subject=request.owner_subject,
        version=1,
        created_at=request.requested_at,
        updated_at=request.requested_at,
    )


def _job_row(
    request: ResearchRunRequest, refs: ResearchRunRefs, run_key: str
) -> JobRow:
    payload = _job_payload(request)
    return JobRow(
        job_id=refs.job_id,
        run_id=refs.run_id,
        job_type="research_pipeline",
        payload=payload,
        payload_hash=stable_payload_hash(payload),
        status=JobStatus.QUEUED.value,
        idempotency_key=f"{run_key}:job",
        not_before=request.requested_at,
        deadline_at=request.requested_at + timedelta(minutes=30),
        attempts=0,
        max_attempts=3,
        attempt_generation=0,
        created_at=request.requested_at,
        updated_at=request.requested_at,
    )


def _existing(
    session: Session,
    run: WorkflowRunRow,
    request: ResearchRunRequest,
    refs: ResearchRunRefs,
    run_key: str,
) -> Result[ResearchRunRefs]:
    jobs = tuple(session.scalars(select(JobRow).where(JobRow.run_id == run.run_id)))
    link = session.scalar(
        select(RunDatasetSnapshotRow).where(RunDatasetSnapshotRow.run_id == run.run_id)
    )
    payload = _job_payload(request)
    if (
        run.run_id != refs.run_id
        or run.run_type != "research_report"
        or run.as_of != request.as_of
        or run.policy_id != request.research_profile_id
        or run.idempotency_key != run_key
        or run.input_hash != request.input_hash
        or run.owner_subject != request.owner_subject
        or len(jobs) != 1
        or jobs[0].job_id != refs.job_id
        or jobs[0].job_type != "research_pipeline"
        or jobs[0].payload_hash != stable_payload_hash(payload)
        or jobs[0].idempotency_key != f"{run_key}:job"
        or link is None
        or link.snapshot_id != request.snapshot_id
    ):
        return _identity_conflict()
    return Success(refs)


def _valid_event_chain(run: WorkflowRunRow, rows: tuple[RunEventRow, ...]) -> bool:
    if not rows:
        return run.version == 1
    previous: str | None = None
    sequence = 2
    for row in rows:
        expected = stable_payload_hash(
            {
                "event_id": str(row.event_id),
                "sequence": row.sequence,
                "previous_hash": previous,
                "payload": row.payload,
            }
        )
        if (
            row.sequence != sequence
            or row.previous_hash != previous
            or row.event_hash != expected
        ):
            return False
        previous = row.event_hash
        sequence += 1
    return rows[-1].sequence == run.version and rows[-1].occurred_at == run.updated_at


def _projection(row: RunEventRow) -> CanonicalRunEvent:
    return CanonicalRunEvent(
        event_id=row.event_id,
        run_id=row.run_id,
        sequence=row.sequence,
        event_type=row.event_type,
        payload=row.payload,
        occurred_at=row.occurred_at,
        event_hash=row.event_hash,
    )


def _identifiers(owner_scope: str, key: str) -> ResearchRunRefs:
    return ResearchRunRefs(
        run_id=uuid5(NAMESPACE_URL, f"stonks:research:{owner_scope}:{key}:run"),
        job_id=uuid5(NAMESPACE_URL, f"stonks:research:{owner_scope}:{key}:job"),
    )


def _job_payload(request: ResearchRunRequest) -> dict[str, object]:
    return request.model_dump(mode="json", exclude={"requested_at"})


def _owner_scope(owner_subject: str) -> str:
    return stable_payload_hash({"owner_subject": owner_subject})[:32]


def _identity_conflict() -> Failure:
    return _failure(ErrorCode.CONFLICT, "Research idempotency identity changed")


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
