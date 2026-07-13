"""Fenced PostgreSQL checkpoints for the canonical paper fund cycle."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
from stonks_agent.domain.job import JobStatus
from stonks_agent.domain.paper_cycle import (
    CancelPaperCycle,
    PaperCycleRunResult,
    PaperCycleRunStatus,
    PaperCycleState,
    RunPaperCycle,
)
from stonks_agent.domain.workflow import WorkflowStatus
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_contracts.common import stable_payload_hash

_SCHEMA = "paper-cycle-checkpoint/1.0.0"
_RESULT_SCHEMA = "paper-fund-cycle-result/1.0.0"
_RETRYABLE = frozenset(
    {
        ErrorCode.DATA_UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
        ErrorCode.TOOL_FAILED,
        ErrorCode.INTERNAL_ERROR,
    }
)
_TERMINAL_EVENTS = frozenset(
    {
        "paper_cycle.completed",
        "paper_cycle.dead_lettered",
        "paper_cycle.cancelled",
    }
)


class _Rejected(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PostgresPaperCycleStore:
    """Core-only store; every mutation validates the active job lease in DB."""

    def __init__(
        self,
        engine: Engine,
        *,
        base_retry_delay: timedelta = timedelta(seconds=1),
    ) -> None:
        if base_retry_delay < timedelta(0) or base_retry_delay > timedelta(hours=1):
            raise ValueError("paper cycle retry delay is invalid")
        self._engine = engine
        self._base_retry_delay = base_retry_delay

    def load(self, command: RunPaperCycle) -> Result[PaperCycleState]:
        try:
            with Session(self._engine) as session, session.begin():
                job = _locked_job(session, command.lease.job_id)
                run = _locked_run(session, command.lease.run_id)
                if job.status == JobStatus.SUCCEEDED.value:
                    _require_completed_authority(job, run, command)
                else:
                    _require_active(job, run, command, session)
                return Success(_load_state(session, job, run, command.cycle_input_hash))
        except _Rejected as error:
            return _failure(error.code, error.message)
        except (SQLAlchemyError, ValueError):
            return _failure(ErrorCode.CONFLICT, "Paper cycle checkpoint is invalid")

    def checkpoint(
        self,
        command: RunPaperCycle,
        state: PaperCycleState,
        *,
        expected_state_hash: str,
    ) -> Result[PaperCycleState]:
        try:
            with Session(self._engine) as session, session.begin():
                job, run, now = _active_authority(session, command)
                current = _load_state(session, job, run, command.cycle_input_hash)
                _require_next_state(current, state, expected_state_hash)
                payload: dict[str, object] = {
                    "schema": _SCHEMA,
                    "job_id": str(job.job_id),
                    "cycle_input_hash": command.cycle_input_hash,
                    "stage": state.completed_stages[-1].value,
                    "state": state.model_dump(mode="json"),
                    "state_hash": state.state_hash,
                    "attempt_generation": command.lease.attempt_generation,
                }
                _append_audit(
                    session,
                    run,
                    job,
                    event_type="paper_cycle.stage_completed",
                    payload=payload,
                    identity=f"stage:{state.completed_stages[-1].value}:{state.state_hash}",
                    occurred_at=now,
                    run_status=WorkflowStatus.RUNNING,
                )
                session.flush()
                return Success(state)
        except _Rejected as error:
            return _failure(error.code, error.message)
        except (IntegrityError, ValueError):
            return _failure(ErrorCode.CONFLICT, "Paper cycle checkpoint conflicted")
        except SQLAlchemyError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Paper cycle checkpoint failed")

    def fail(
        self,
        command: RunPaperCycle,
        error: StructuredError,
    ) -> Result[PaperCycleRunResult]:
        try:
            with Session(self._engine) as session, session.begin():
                job, run, now = _active_authority(session, command)
                state = _load_state(session, job, run, command.cycle_input_hash)
                retry_at = now + self._retry_delay(job.attempts)
                retry = (
                    error.code in _RETRYABLE
                    and job.attempts < job.max_attempts
                    and retry_at < job.deadline_at
                )
                status = (
                    PaperCycleRunStatus.RETRY_SCHEDULED
                    if retry
                    else PaperCycleRunStatus.DEAD_LETTERED
                )
                event_type = (
                    "paper_cycle.retry_scheduled"
                    if retry
                    else "paper_cycle.dead_lettered"
                )
                payload: dict[str, object] = {
                    "schema": _SCHEMA,
                    "job_id": str(job.job_id),
                    "cycle_input_hash": command.cycle_input_hash,
                    "state_hash": state.state_hash,
                    "error_code": error.code.value,
                    "attempt_generation": command.lease.attempt_generation,
                    "retry_at": retry_at.isoformat() if retry else None,
                }
                _append_audit(
                    session,
                    run,
                    job,
                    event_type=event_type,
                    payload=payload,
                    identity=f"{status.value}:{command.lease.attempt_generation}",
                    occurred_at=now,
                    run_status=(
                        WorkflowStatus.RUNNING if retry else WorkflowStatus.FAILED
                    ),
                )
                _mark_failed_attempt(job, error.code, now, retry_at if retry else None)
                session.flush()
                return Success(
                    PaperCycleRunResult(
                        run_id=run.run_id,
                        status=status,
                        state=state,
                        result_artifact_hash=None,
                        error_code=error.code.value,
                    )
                )
        except _Rejected as rejected:
            return _failure(rejected.code, rejected.message)
        except (IntegrityError, ValueError):
            return _failure(ErrorCode.CONFLICT, "Paper cycle failure audit conflicted")
        except SQLAlchemyError:
            return _failure(
                ErrorCode.INTERNAL_ERROR, "Paper cycle failure audit failed"
            )

    def complete(
        self,
        command: RunPaperCycle,
        state: PaperCycleState,
        artifact: ArtifactManifest,
    ) -> Result[PaperCycleRunResult]:
        try:
            with Session(self._engine) as session, session.begin():
                job = _locked_job(session, command.lease.job_id)
                run = _locked_run(session, command.lease.run_id)
                if job.status == JobStatus.SUCCEEDED.value:
                    return _completed_result(
                        session, command, job, run, state, artifact
                    )
                _, _, now = _require_active(job, run, command, session)
                current = _load_state(session, job, run, command.cycle_input_hash)
                if current != state or not state.complete:
                    raise _Rejected(
                        ErrorCode.CONFLICT, "Paper cycle is not ready to complete"
                    )
                artifact_row = _register_artifact(
                    session,
                    artifact,
                    state,
                    now,
                )
                payload = _completion_payload(job, command, state, artifact_row)
                _append_audit(
                    session,
                    run,
                    job,
                    event_type="paper_cycle.completed",
                    payload=payload,
                    identity=f"completed:{command.lease.attempt_generation}",
                    occurred_at=now,
                    run_status=WorkflowStatus.SUCCEEDED,
                )
                job.status = JobStatus.SUCCEEDED.value
                job.result_artifact_hash = artifact.content_hash
                job.last_error = None
                job.updated_at = now
                session.flush()
                return Success(
                    PaperCycleRunResult(
                        run_id=run.run_id,
                        status=PaperCycleRunStatus.SUCCEEDED,
                        state=state,
                        result_artifact_hash=artifact.content_hash,
                        error_code=None,
                    )
                )
        except _Rejected as error:
            return _failure(error.code, error.message)
        except (IntegrityError, ValueError):
            return _failure(ErrorCode.CONFLICT, "Paper cycle completion conflicted")
        except SQLAlchemyError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Paper cycle completion failed")

    def cancel(
        self,
        command: CancelPaperCycle,
    ) -> Result[PaperCycleRunResult]:
        try:
            with Session(self._engine) as session, session.begin():
                run = _locked_run(session, command.run_id)
                jobs = session.scalars(
                    select(JobRow)
                    .where(
                        JobRow.run_id == command.run_id,
                        JobRow.job_type == "paper_fund_cycle",
                    )
                    .with_for_update()
                ).all()
                if len(jobs) != 1:
                    raise _Rejected(ErrorCode.CONFLICT, "Paper cycle job is invalid")
                job = jobs[0]
                state = _load_state(session, job, run, run.input_hash)
                if run.status == WorkflowStatus.CANCELLED.value:
                    if _cancelled_is_exact(session, run, job, command):
                        return _cancelled_result(run, state, command.reason_code)
                    raise _Rejected(
                        ErrorCode.CONFLICT,
                        "Cancelled paper cycle command conflicts",
                    )
                if (
                    run.version != command.expected_version
                    or run.status
                    not in {
                        WorkflowStatus.PENDING.value,
                        WorkflowStatus.RUNNING.value,
                        WorkflowStatus.DEGRADED.value,
                    }
                    or job.status
                    not in {JobStatus.QUEUED.value, JobStatus.LEASED.value}
                ):
                    raise _Rejected(
                        ErrorCode.CONFLICT, "Paper cycle cannot be cancelled"
                    )
                now = _database_now(session)
                payload: dict[str, object] = {
                    "schema": _SCHEMA,
                    "job_id": str(job.job_id),
                    "cycle_input_hash": run.input_hash,
                    "state_hash": state.state_hash,
                    "actor": command.actor,
                    "reason_code": command.reason_code,
                }
                _append_audit(
                    session,
                    run,
                    job,
                    event_type="paper_cycle.cancelled",
                    payload=payload,
                    identity=f"cancelled:{command.expected_version + 1}",
                    occurred_at=now,
                    run_status=WorkflowStatus.CANCELLED,
                )
                job.status = JobStatus.CANCELLED.value
                job.attempt_nonce = None
                job.lease_owner = None
                job.lease_until = None
                job.last_error = {"code": command.reason_code}
                job.updated_at = now
                session.flush()
                return _cancelled_result(run, state, command.reason_code)
        except _Rejected as error:
            return _failure(error.code, error.message)
        except (IntegrityError, ValueError):
            return _failure(ErrorCode.CONFLICT, "Paper cycle cancellation conflicted")
        except SQLAlchemyError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Paper cycle cancellation failed")

    def _retry_delay(self, attempts: int) -> timedelta:
        multiplier = min(2 ** max(0, attempts - 1), 300)
        return timedelta(seconds=self._base_retry_delay.total_seconds() * multiplier)


def _active_authority(
    session: Session,
    command: RunPaperCycle,
) -> tuple[JobRow, WorkflowRunRow, datetime]:
    job = _locked_job(session, command.lease.job_id)
    run = _locked_run(session, command.lease.run_id)
    return _require_active(job, run, command, session)


def _require_active(
    job: JobRow,
    run: WorkflowRunRow,
    command: RunPaperCycle,
    session: Session,
) -> tuple[JobRow, WorkflowRunRow, datetime]:
    lease = command.lease
    now = _database_now(session)
    valid = (
        job.run_id == lease.run_id
        and job.job_type == "paper_fund_cycle"
        and job.status == JobStatus.LEASED.value
        and job.attempt_generation == lease.attempt_generation
        and job.attempt_nonce == lease.attempt_nonce
        and job.lease_owner == lease.lease_owner
        and job.lease_until is not None
        and job.lease_until > now
        and job.deadline_at > now
        and stable_payload_hash(job.payload) == job.payload_hash
        and job.payload.get("cycle_input_hash") == command.cycle_input_hash
        and run.run_id == lease.run_id
        and run.run_type == "paper_fund_cycle"
        and run.input_hash == command.cycle_input_hash
        and run.status in {WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value}
    )
    if not valid:
        raise _Rejected(ErrorCode.CONFLICT, "Paper cycle lease is stale or invalid")
    return job, run, now


def _require_completed_authority(
    job: JobRow,
    run: WorkflowRunRow,
    command: RunPaperCycle,
) -> None:
    lease = command.lease
    valid = (
        job.run_id == lease.run_id
        and job.job_type == "paper_fund_cycle"
        and job.attempt_generation == lease.attempt_generation
        and job.attempt_nonce == lease.attempt_nonce
        and job.lease_owner == lease.lease_owner
        and job.result_artifact_hash is not None
        and stable_payload_hash(job.payload) == job.payload_hash
        and job.payload.get("cycle_input_hash") == command.cycle_input_hash
        and run.run_id == lease.run_id
        and run.run_type == "paper_fund_cycle"
        and run.input_hash == command.cycle_input_hash
        and run.status == WorkflowStatus.SUCCEEDED.value
    )
    if not valid:
        raise _Rejected(
            ErrorCode.CONFLICT, "Completed paper cycle authority is invalid"
        )


def _locked_job(session: Session, job_id: UUID) -> JobRow:
    row = session.scalar(
        select(JobRow).where(JobRow.job_id == job_id).with_for_update()
    )
    if row is None:
        raise _Rejected(ErrorCode.NOT_FOUND, "Paper cycle job was not found")
    return row


def _locked_run(session: Session, run_id: UUID) -> WorkflowRunRow:
    row = session.scalar(
        select(WorkflowRunRow).where(WorkflowRunRow.run_id == run_id).with_for_update()
    )
    if row is None:
        raise _Rejected(ErrorCode.NOT_FOUND, "Paper cycle run was not found")
    return row


def _database_now(session: Session) -> datetime:
    value = session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _Rejected(
            ErrorCode.INTERNAL_ERROR, "Paper cycle database time is invalid"
        )
    return value


def _load_state(
    session: Session,
    job: JobRow,
    run: WorkflowRunRow,
    cycle_input_hash: str,
) -> PaperCycleState:
    events = tuple(
        session.scalars(
            select(RunEventRow)
            .where(RunEventRow.run_id == run.run_id)
            .order_by(RunEventRow.sequence)
        )
    )
    state = PaperCycleState.genesis(run.run_id, cycle_input_hash)
    previous_hash: str | None = None
    expected_sequence = 2
    for event in events:
        _validate_event(session, run, event, expected_sequence, previous_hash)
        if event.event_type == "paper_cycle.stage_completed":
            candidate = _checkpoint_state(event, job, cycle_input_hash)
            _require_next_state(state, candidate, state.state_hash)
            state = candidate
        elif event.event_type not in {
            "paper_cycle.retry_scheduled",
            *_TERMINAL_EVENTS,
        }:
            raise _Rejected(ErrorCode.CONFLICT, "Paper cycle event type is invalid")
        previous_hash = event.event_hash
        expected_sequence += 1
    if run.version != expected_sequence - 1:
        raise _Rejected(ErrorCode.CONFLICT, "Paper cycle run/event sequence diverged")
    if events and run.updated_at != events[-1].occurred_at:
        raise _Rejected(ErrorCode.CONFLICT, "Paper cycle run/event time diverged")
    return state


def _validate_event(
    session: Session,
    run: WorkflowRunRow,
    event: RunEventRow,
    sequence: int,
    previous_hash: str | None,
) -> None:
    if (
        event.sequence != sequence
        or event.previous_hash != previous_hash
        or event.event_hash
        != _event_hash(event.event_id, sequence, previous_hash, event.payload)
    ):
        raise _Rejected(ErrorCode.CONFLICT, "Paper cycle event chain is invalid")
    outbox = session.scalar(
        select(OutboxRow).where(
            OutboxRow.aggregate_type == "run",
            OutboxRow.aggregate_id == str(run.run_id),
            OutboxRow.sequence == sequence,
        )
    )
    if (
        outbox is None
        or outbox.topic != event.event_type
        or outbox.payload != event.payload
        or outbox.created_at != event.occurred_at
    ):
        raise _Rejected(ErrorCode.CONFLICT, "Paper cycle outbox graph is invalid")


def _checkpoint_state(
    event: RunEventRow,
    job: JobRow,
    cycle_input_hash: str,
) -> PaperCycleState:
    payload = event.payload
    if (
        payload.get("schema") != _SCHEMA
        or payload.get("job_id") != str(job.job_id)
        or payload.get("cycle_input_hash") != cycle_input_hash
        or payload.get("state_hash") is None
        or payload.get("stage") is None
        or not isinstance(payload.get("state"), dict)
    ):
        raise _Rejected(ErrorCode.CONFLICT, "Paper cycle checkpoint payload is invalid")
    state = PaperCycleState.model_validate(payload["state"])
    if (
        payload["state_hash"] != state.state_hash
        or payload["stage"] != state.completed_stages[-1].value
    ):
        raise _Rejected(ErrorCode.CONFLICT, "Paper cycle checkpoint binding changed")
    return state


def _require_next_state(
    current: PaperCycleState,
    candidate: PaperCycleState,
    expected_state_hash: str,
) -> None:
    if (
        current.state_hash != expected_state_hash
        or candidate.run_id != current.run_id
        or candidate.cycle_input_hash != current.cycle_input_hash
        or len(candidate.outputs) != len(current.outputs) + 1
        or candidate.outputs[:-1] != current.outputs
        or candidate.outputs[-1].stage is not current.next_stage
    ):
        raise _Rejected(ErrorCode.CONFLICT, "Paper cycle state CAS failed")


def _append_audit(
    session: Session,
    run: WorkflowRunRow,
    job: JobRow,
    *,
    event_type: str,
    payload: dict[str, object],
    identity: str,
    occurred_at: datetime,
    run_status: WorkflowStatus,
) -> None:
    sequence = run.version + 1
    previous_hash = session.scalar(
        select(RunEventRow.event_hash)
        .where(RunEventRow.run_id == run.run_id)
        .order_by(RunEventRow.sequence.desc())
        .limit(1)
    )
    event_id = uuid5(
        NAMESPACE_URL,
        f"stonks:paper-cycle:{job.job_id}:event:{identity}",
    )
    outbox_id = uuid5(
        NAMESPACE_URL,
        f"stonks:paper-cycle:{job.job_id}:outbox:{identity}",
    )
    event_hash = _event_hash(event_id, sequence, previous_hash, payload)
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
            idempotency_key=f"paper-cycle:{job.job_id}:{identity}",
            created_at=occurred_at,
            not_before=occurred_at,
            attempts=0,
        )
    )
    run.status = run_status.value
    run.version = sequence
    run.updated_at = occurred_at


def _mark_failed_attempt(
    job: JobRow,
    code: ErrorCode,
    now: datetime,
    retry_at: datetime | None,
) -> None:
    retry = retry_at is not None
    job.status = JobStatus.QUEUED.value if retry else JobStatus.DEAD_LETTER.value
    if retry_at is not None:
        job.not_before = retry_at
    job.attempt_nonce = None
    job.lease_owner = None
    job.lease_until = None
    job.last_error = {
        "code": code.value,
        "attempt_generation": job.attempt_generation,
    }
    job.updated_at = now


def _register_artifact(
    session: Session,
    manifest: ArtifactManifest,
    state: PaperCycleState,
    now: datetime,
) -> ArtifactManifestRow:
    attributes = dict(manifest.metadata.attributes)
    valid = (
        1 <= manifest.size_bytes <= 1_048_576
        and manifest.finalized_at <= now
        and manifest.metadata.media_type == "application/json"
        and manifest.metadata.license_tag == "Apache-2.0"
        and manifest.metadata.sensitivity.value == "internal"
        and manifest.metadata.source == "stonks-agent-paper-cycle"
        and attributes
        == {
            "run_id": str(state.run_id),
            "schema": _RESULT_SCHEMA,
            "state_hash": state.state_hash,
        }
    )
    if not valid:
        raise _Rejected(ErrorCode.CONFLICT, "Paper cycle artifact metadata is invalid")
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
        return candidate
    fields = (
        "size_bytes",
        "media_type",
        "license_tag",
        "sensitivity",
        "source",
        "finalized_at",
        "storage_uri",
        "metadata_payload",
    )
    if any(getattr(existing, field) != getattr(candidate, field) for field in fields):
        raise _Rejected(ErrorCode.CONFLICT, "Paper cycle artifact identity changed")
    return existing


def _completion_payload(
    job: JobRow,
    command: RunPaperCycle,
    state: PaperCycleState,
    artifact: ArtifactManifestRow,
) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "job_id": str(job.job_id),
        "cycle_input_hash": command.cycle_input_hash,
        "state_hash": state.state_hash,
        "result_artifact_hash": artifact.content_hash,
        "result_artifact_identity_hash": stable_payload_hash(
            {
                "content_hash": artifact.content_hash,
                "size_bytes": artifact.size_bytes,
                "metadata": artifact.metadata_payload,
                "storage_uri": artifact.storage_uri,
            }
        ),
        "attempt_generation": command.lease.attempt_generation,
    }


def _completed_result(
    session: Session,
    command: RunPaperCycle,
    job: JobRow,
    run: WorkflowRunRow,
    state: PaperCycleState,
    artifact: ArtifactManifest,
) -> Result[PaperCycleRunResult]:
    current = _load_state(session, job, run, command.cycle_input_hash)
    event = session.scalar(
        select(RunEventRow)
        .where(
            RunEventRow.run_id == run.run_id,
            RunEventRow.event_type == "paper_cycle.completed",
        )
        .order_by(RunEventRow.sequence.desc())
        .limit(1)
    )
    row = session.get(ArtifactManifestRow, artifact.content_hash)
    valid = (
        current == state
        and state.complete
        and run.status == WorkflowStatus.SUCCEEDED.value
        and job.result_artifact_hash == artifact.content_hash
        and job.attempt_generation == command.lease.attempt_generation
        and job.attempt_nonce == command.lease.attempt_nonce
        and job.lease_owner == command.lease.lease_owner
        and event is not None
        and row is not None
        and event.payload == _completion_payload(job, command, state, row)
    )
    if not valid:
        return _failure(ErrorCode.CONFLICT, "Completed paper cycle graph is invalid")
    return Success(
        PaperCycleRunResult(
            run_id=run.run_id,
            status=PaperCycleRunStatus.SUCCEEDED,
            state=state,
            result_artifact_hash=artifact.content_hash,
            error_code=None,
        )
    )


def _cancelled_result(
    run: WorkflowRunRow,
    state: PaperCycleState,
    reason_code: str,
) -> Success[PaperCycleRunResult]:
    return Success(
        PaperCycleRunResult(
            run_id=run.run_id,
            status=PaperCycleRunStatus.CANCELLED,
            state=state,
            result_artifact_hash=None,
            error_code=reason_code,
        )
    )


def _cancelled_is_exact(
    session: Session,
    run: WorkflowRunRow,
    job: JobRow,
    command: CancelPaperCycle,
) -> bool:
    event = session.scalar(
        select(RunEventRow)
        .where(
            RunEventRow.run_id == run.run_id,
            RunEventRow.event_type == "paper_cycle.cancelled",
        )
        .order_by(RunEventRow.sequence.desc())
        .limit(1)
    )
    return (
        event is not None
        and run.version == command.expected_version + 1
        and job.status == JobStatus.CANCELLED.value
        and event.payload.get("actor") == command.actor
        and event.payload.get("reason_code") == command.reason_code
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


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
