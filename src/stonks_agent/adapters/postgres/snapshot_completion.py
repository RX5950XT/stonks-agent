from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    ArtifactManifestRow,
    DatasetSnapshotEvidenceRow,
    DatasetSnapshotRow,
    EvidenceItemRow,
    JobRow,
    OutboxRow,
    RunDatasetSnapshotRow,
    RunEventRow,
    WorkflowRunRow,
)
from stonks_agent.adapters.postgres.snapshot_lifecycle import (
    preflight_snapshot_lease,
    record_snapshot_failure,
)
from stonks_agent.adapters.postgres.snapshot_retry_audit import (
    completed_snapshot_receipt,
)
from stonks_agent.domain.data_quality import ProviderDataState
from stonks_agent.domain.dataset_snapshot import (
    CanonicalDatasetSnapshotManifest,
    SnapshotManifestEntry,
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
    CompleteSnapshotJob,
    CreateSnapshotRequest,
    FailSnapshotJob,
    SnapshotAttemptFailureReceipt,
    SnapshotCompletionReceipt,
    snapshot_manifest_is_authorized,
)
from stonks_agent.domain.workflow import WorkflowStatus
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.market_data import DataQuality, DataQualityStatus


@dataclass(frozen=True)
class _PreparedRows:
    artifacts: tuple[ArtifactManifestRow, ...]
    evidence: tuple[EvidenceItemRow, ...]
    snapshot: DatasetSnapshotRow | None
    snapshot_links: tuple[DatasetSnapshotEvidenceRow, ...]
    run_link: RunDatasetSnapshotRow | None


class _CanonicalConflict(Exception):
    pass


_EVIDENCE_FIELDS = (
    "subject",
    "kind",
    "event_time",
    "published_at",
    "available_at",
    "observed_at",
    "as_of",
    "availability_certainty",
    "strict_point_in_time",
    "source",
    "provider",
    "source_url",
    "content_hash",
    "raw_artifact_hash",
    "quality_state",
    "quality",
    "sensitivity",
    "license_tag",
    "redistribution_tag",
    "payload",
    "expires_at",
    "transformation_version",
    "untrusted_content",
)


class PostgresSnapshotCompletionStore:
    """Validate fencing before one transaction writes any canonical row."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def preflight(
        self,
        lease: JobLease,
        *,
        now: datetime,
        policy: ProviderPolicy,
    ) -> Result[CreateSnapshotRequest]:
        return preflight_snapshot_lease(self._engine, lease, now=now, policy=policy)

    def fail(
        self,
        request: FailSnapshotJob,
        *,
        now: datetime,
        policy: ProviderPolicy,
    ) -> Result[SnapshotAttemptFailureReceipt]:
        return record_snapshot_failure(self._engine, request, now=now, policy=policy)

    def complete(
        self,
        request: CompleteSnapshotJob,
        *,
        now: datetime,
        raw_artifact: ArtifactManifest,
        manifest_artifact: ArtifactManifest,
        policy: ProviderPolicy,
    ) -> Result[SnapshotCompletionReceipt]:
        if now.tzinfo is None or now.utcoffset() is None:
            return _failure(ErrorCode.INVALID_INPUT, "Completion time is invalid")
        try:
            with (
                Session(
                    self._engine,
                    expire_on_commit=False,
                ) as session,
                session.begin(),
            ):
                return _complete_in_session(
                    session, request, raw_artifact, manifest_artifact, policy
                )
        except (_CanonicalConflict, IntegrityError, ValidationError, ValueError):
            return _failure(
                ErrorCode.CONFLICT,
                "Snapshot completion conflicts with canonical state",
            )
        except SQLAlchemyError:
            return _failure(
                ErrorCode.INTERNAL_ERROR,
                "Snapshot completion storage failed",
            )


def _complete_in_session(
    session: Session,
    request: CompleteSnapshotJob,
    raw_artifact: ArtifactManifest,
    manifest_artifact: ArtifactManifest,
    policy: ProviderPolicy,
) -> Result[SnapshotCompletionReceipt]:
    locked_job = _locked_job(session, request.job_id)
    if isinstance(locked_job, Failure):
        return locked_job
    job = locked_job.value
    database_now = _database_now(session)
    if job.status == JobStatus.SUCCEEDED.value:
        return completed_snapshot_receipt(
            session,
            job,
            request,
            raw_artifact=raw_artifact,
            manifest_artifact=manifest_artifact,
            policy=policy,
        )
    lease = _validate_active_lease(job, request, database_now)
    if isinstance(lease, Failure):
        return lease
    run = _locked_run(session, job)
    if isinstance(run, Failure):
        return run
    context = _validate_completion_context(
        job,
        run.value,
        request,
        raw_artifact,
        manifest_artifact,
        database_now,
        policy,
    )
    if isinstance(context, Failure):
        return context
    prepared = _prepare_rows(
        session,
        run.value,
        request,
        raw_artifact,
        manifest_artifact,
        database_now,
    )
    _insert_prepared(session, prepared)
    receipt = _finish_job(session, job, run.value, request, database_now)
    session.flush()
    return Success(receipt)


def _locked_job(session: Session, job_id: UUID) -> Result[JobRow]:
    job = session.scalar(
        select(JobRow).where(JobRow.job_id == job_id).with_for_update()
    )
    if job is None:
        return _failure(ErrorCode.NOT_FOUND, "Job was not found")
    return Success(job)


def _database_now(session: Session) -> datetime:
    value = session.scalar(select(func.clock_timestamp()))
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise _CanonicalConflict
    return value


def _validate_active_lease(
    job: JobRow,
    request: CompleteSnapshotJob,
    now: datetime,
) -> Result[bool]:
    invalid = (
        job.status != JobStatus.LEASED.value
        or job.job_type != "create_snapshot"
        or job.lease_owner != request.worker_id
        or job.attempt_generation != request.attempt_generation
        or job.attempt_nonce != request.attempt_nonce
        or job.lease_until is None
        or job.lease_until <= now
        or job.deadline_at <= now
    )
    if invalid:
        return _failure(ErrorCode.CONFLICT, "Job lease is stale or invalid")
    if job.result_artifact_hash is not None:
        return _failure(ErrorCode.CONFLICT, "Snapshot job state is invalid")
    return Success(True)


def _locked_run(
    session: Session,
    job: JobRow,
) -> Result[WorkflowRunRow]:
    run = session.scalar(
        select(WorkflowRunRow)
        .where(WorkflowRunRow.run_id == job.run_id)
        .with_for_update()
    )
    if run is None:
        return _failure(ErrorCode.CONFLICT, "Owning run was not found")
    return Success(run)


def _validate_completion_context(
    job: JobRow,
    run: WorkflowRunRow,
    request: CompleteSnapshotJob,
    raw_artifact: ArtifactManifest,
    manifest_artifact: ArtifactManifest,
    now: datetime,
    policy: ProviderPolicy,
) -> Result[bool]:
    snapshot = request.snapshot
    manifest = snapshot.manifest
    try:
        queued = CreateSnapshotRequest.model_validate(job.payload)
    except ValidationError:
        return _failure(ErrorCode.CONFLICT, "Snapshot job payload is invalid")
    if stable_payload_hash(job.payload) != job.payload_hash:
        return _failure(ErrorCode.CONFLICT, "Snapshot job payload hash is invalid")
    if (
        run.run_type != "data_snapshot"
        or run.status
        not in {
            WorkflowStatus.PENDING.value,
            WorkflowStatus.RUNNING.value,
        }
        or queued.input_hash != run.input_hash
        or queued.as_of != run.as_of
        or queued.provider_policy_id != run.policy_id
        or not snapshot_manifest_is_authorized(queued, manifest, policy)
        or manifest.request_hash != queued.request_hash
        or manifest.as_of != run.as_of
        or manifest.provider_policy_id != run.policy_id
        or manifest.provider_observed_at > now
    ):
        return _failure(ErrorCode.CONFLICT, "Snapshot does not match its owning run")
    if not _artifact_context_is_valid(
        manifest,
        raw_artifact,
        manifest_artifact,
        now,
    ):
        return _failure(ErrorCode.CONFLICT, "Snapshot artifact metadata is invalid")
    return Success(True)


def _artifact_context_is_valid(
    manifest: CanonicalDatasetSnapshotManifest,
    raw: ArtifactManifest,
    archived_manifest: ArtifactManifest,
    now: datetime,
) -> bool:
    raw_attributes = _unique_attributes(raw)
    manifest_attributes = _unique_attributes(archived_manifest)
    return (
        raw.content_hash == manifest.raw_artifact_hash
        and raw.metadata.media_type == manifest.raw_media_type
        and raw.metadata.license_tag == manifest.license_tag
        and raw.metadata.sensitivity is manifest.sensitivity
        and raw.metadata.source == manifest.provider
        and raw.finalized_at == manifest.provider_observed_at
        and raw.finalized_at <= now
        and raw_attributes
        == {
            "endpoint": manifest.endpoint,
            "provider_version": manifest.provider_version,
            "redistribution_tag": manifest.redistribution_tag,
        }
        and archived_manifest.content_hash == manifest.content_hash
        and archived_manifest.size_bytes == len(manifest.canonical_bytes())
        and archived_manifest.metadata.media_type == "application/json"
        and archived_manifest.metadata.license_tag == "Apache-2.0"
        and archived_manifest.metadata.sensitivity.value == "internal"
        and archived_manifest.metadata.source == "stonks-agent"
        and archived_manifest.finalized_at == manifest.provider_observed_at
        and archived_manifest.finalized_at <= now
        and manifest_attributes
        == {
            "provider": manifest.provider,
            "raw_artifact_hash": manifest.raw_artifact_hash,
            "schema": "canonical-dataset-snapshot/1.0.0",
        }
    )


def _unique_attributes(artifact: ArtifactManifest) -> dict[str, str] | None:
    attributes = artifact.metadata.attributes
    result = dict(attributes)
    return result if len(result) == len(attributes) else None


def _prepare_rows(
    session: Session,
    run: WorkflowRunRow,
    request: CompleteSnapshotJob,
    raw_artifact: ArtifactManifest,
    manifest_artifact: ArtifactManifest,
    now: datetime,
) -> _PreparedRows:
    artifact_rows = _new_artifact_rows(
        session,
        (raw_artifact, manifest_artifact),
    )
    evidence_rows = _new_evidence_rows(session, request, now)
    snapshot_row = _prepare_snapshot(session, request, now)
    snapshot_links = _new_snapshot_links(session, request, now)
    run_link = _prepare_run_link(
        session,
        run.run_id,
        request.snapshot.snapshot_id,
        now,
    )
    _require_no_completion_audit_rows(session, request)
    return _PreparedRows(
        artifacts=artifact_rows,
        evidence=evidence_rows,
        snapshot=snapshot_row,
        snapshot_links=snapshot_links,
        run_link=run_link,
    )


def _new_artifact_rows(
    session: Session,
    artifacts: tuple[ArtifactManifest, ...],
) -> tuple[ArtifactManifestRow, ...]:
    rows: list[ArtifactManifestRow] = []
    for artifact in artifacts:
        row = _prepare_artifact(session, artifact)
        if row is not None:
            rows.append(row)
    return tuple(rows)


def _new_evidence_rows(
    session: Session,
    request: CompleteSnapshotJob,
    now: datetime,
) -> tuple[EvidenceItemRow, ...]:
    rows: list[EvidenceItemRow] = []
    for entry in request.snapshot.manifest.entries:
        row = _prepare_evidence(session, request.snapshot.manifest, entry, now)
        if row is not None:
            rows.append(row)
    return tuple(rows)


def _new_snapshot_links(
    session: Session,
    request: CompleteSnapshotJob,
    now: datetime,
) -> tuple[DatasetSnapshotEvidenceRow, ...]:
    rows: list[DatasetSnapshotEvidenceRow] = []
    for evidence_id in request.snapshot.evidence_refs:
        row = _prepare_snapshot_link(
            session,
            request.snapshot.snapshot_id,
            evidence_id,
            now,
        )
        if row is not None:
            rows.append(row)
    return tuple(rows)


def _prepare_artifact(
    session: Session,
    artifact: ArtifactManifest,
) -> ArtifactManifestRow | None:
    candidate = ArtifactManifestRow(
        content_hash=artifact.content_hash,
        size_bytes=artifact.size_bytes,
        media_type=artifact.metadata.media_type,
        license_tag=artifact.metadata.license_tag,
        sensitivity=artifact.metadata.sensitivity.value,
        source=artifact.metadata.source,
        finalized_at=artifact.finalized_at,
        storage_uri=artifact.storage_uri,
        metadata_payload=artifact.metadata.model_dump(mode="json"),
    )
    existing = session.get(ArtifactManifestRow, artifact.content_hash)
    if existing is None:
        return candidate
    _require_same(
        existing,
        candidate,
        (
            "size_bytes",
            "media_type",
            "license_tag",
            "sensitivity",
            "source",
            "finalized_at",
            "storage_uri",
            "metadata_payload",
        ),
    )
    return None


def _prepare_evidence(
    session: Session,
    manifest: CanonicalDatasetSnapshotManifest,
    entry: SnapshotManifestEntry,
    now: datetime,
) -> EvidenceItemRow | None:
    candidate = _evidence_candidate(manifest, entry, now)
    existing = session.get(EvidenceItemRow, entry.evidence_id)
    hash_owner = session.scalar(
        select(EvidenceItemRow.evidence_id).where(
            EvidenceItemRow.content_hash == candidate.content_hash
        )
    )
    if existing is None:
        if hash_owner is not None:
            raise _CanonicalConflict
        return candidate
    if hash_owner != entry.evidence_id:
        raise _CanonicalConflict
    _require_same(existing, candidate, _EVIDENCE_FIELDS)
    return None


def _evidence_candidate(
    manifest: CanonicalDatasetSnapshotManifest,
    entry: SnapshotManifestEntry,
    now: datetime,
) -> EvidenceItemRow:
    quality = _evidence_quality(manifest)
    return EvidenceItemRow(
        evidence_id=entry.evidence_id,
        subject=entry.subject,
        kind=entry.kind,
        event_time=entry.timeline.event_time,
        published_at=entry.timeline.published_at,
        available_at=entry.timeline.available_at,
        observed_at=entry.timeline.observed_at,
        as_of=entry.timeline.as_of,
        availability_certainty=entry.timeline.availability_certainty.value,
        strict_point_in_time=entry.timeline.strict_point_in_time,
        source=entry.provider,
        provider=entry.provider,
        source_url=None,
        content_hash=stable_payload_hash(entry),
        raw_artifact_hash=entry.raw_artifact_hash,
        quality_state=quality.status.value,
        quality=quality.model_dump(mode="json"),
        sensitivity=entry.sensitivity.value,
        license_tag=entry.license_tag,
        redistribution_tag=entry.redistribution_tag,
        payload=entry.payload,
        expires_at=None,
        transformation_version=entry.provider_version,
        untrusted_content=True,
        created_at=now,
    )


def _evidence_quality(
    manifest: CanonicalDatasetSnapshotManifest,
) -> DataQuality:
    statuses = {
        ProviderDataState.AVAILABLE: DataQualityStatus.AVAILABLE,
        ProviderDataState.STALE: DataQualityStatus.STALE,
        ProviderDataState.PARTIAL: DataQualityStatus.PARTIAL,
    }
    status = statuses.get(manifest.provider_state)
    if status is None:
        raise _CanonicalConflict
    return DataQuality(
        status=status,
        completeness=manifest.completeness,
        warnings=manifest.reasons,
    )


def _prepare_snapshot(
    session: Session,
    request: CompleteSnapshotJob,
    now: datetime,
) -> DatasetSnapshotRow | None:
    snapshot = request.snapshot
    candidate = DatasetSnapshotRow(
        snapshot_id=snapshot.snapshot_id,
        as_of=snapshot.manifest.as_of,
        cutoff_at=snapshot.manifest.as_of,
        provider_policy_id=snapshot.manifest.provider_policy_id,
        manifest_artifact_hash=snapshot.manifest_artifact_hash,
        content_hash=snapshot.manifest.content_hash,
        manifest=snapshot.manifest.model_dump(mode="json"),
        created_at=now,
    )
    existing = session.get(DatasetSnapshotRow, snapshot.snapshot_id)
    hash_owner = session.scalar(
        select(DatasetSnapshotRow.snapshot_id).where(
            DatasetSnapshotRow.content_hash == snapshot.manifest.content_hash
        )
    )
    if existing is None:
        if hash_owner is not None:
            raise _CanonicalConflict
        return candidate
    if hash_owner != snapshot.snapshot_id:
        raise _CanonicalConflict
    _require_same(
        existing,
        candidate,
        (
            "as_of",
            "cutoff_at",
            "provider_policy_id",
            "manifest_artifact_hash",
            "content_hash",
            "manifest",
        ),
    )
    return None


def _prepare_snapshot_link(
    session: Session,
    snapshot_id: UUID,
    evidence_id: UUID,
    now: datetime,
) -> DatasetSnapshotEvidenceRow | None:
    key = {"snapshot_id": snapshot_id, "evidence_id": evidence_id}
    if session.get(DatasetSnapshotEvidenceRow, key) is not None:
        return None
    return DatasetSnapshotEvidenceRow(
        snapshot_id=snapshot_id,
        evidence_id=evidence_id,
        created_at=now,
    )


def _prepare_run_link(
    session: Session,
    run_id: UUID,
    snapshot_id: UUID,
    now: datetime,
) -> RunDatasetSnapshotRow | None:
    existing = session.get(RunDatasetSnapshotRow, run_id)
    if existing is None:
        return RunDatasetSnapshotRow(
            run_id=run_id,
            snapshot_id=snapshot_id,
            created_at=now,
        )
    if existing.snapshot_id != snapshot_id:
        raise _CanonicalConflict
    return None


def _require_no_completion_audit_rows(
    session: Session,
    request: CompleteSnapshotJob,
) -> None:
    if (
        session.get(RunEventRow, _event_id(request)) is not None
        or session.get(OutboxRow, _outbox_id(request)) is not None
    ):
        raise _CanonicalConflict


def _insert_prepared(session: Session, rows: _PreparedRows) -> None:
    session.add_all(rows.artifacts)
    session.flush()
    session.add_all(rows.evidence)
    session.flush()
    if rows.snapshot is not None:
        session.add(rows.snapshot)
        session.flush()
    session.add_all(rows.snapshot_links)
    session.flush()
    if rows.run_link is not None:
        session.add(rows.run_link)
        session.flush()


def _finish_job(
    session: Session,
    job: JobRow,
    run: WorkflowRunRow,
    request: CompleteSnapshotJob,
    now: datetime,
) -> SnapshotCompletionReceipt:
    sequence = run.version + 1
    previous_hash = session.scalar(
        select(RunEventRow.event_hash)
        .where(RunEventRow.run_id == run.run_id)
        .order_by(RunEventRow.sequence.desc())
        .limit(1)
    )
    payload = _event_payload(job, request)
    event_id = _event_id(request)
    outbox_id = _outbox_id(request)
    event_hash = stable_payload_hash(
        {
            "event_id": str(event_id),
            "sequence": sequence,
            "previous_hash": previous_hash,
            "payload": payload,
        }
    )
    session.add(
        _completion_event(
            event_id, run.run_id, sequence, payload, now, previous_hash, event_hash
        )
    )
    session.add(
        _completion_outbox(outbox_id, run.run_id, sequence, payload, job, request, now)
    )
    _mark_completed(job, run, request, sequence, now)
    return _completion_receipt(job, run, request, event_id, outbox_id, sequence, now)


def _completion_event(
    event_id: UUID,
    run_id: UUID,
    sequence: int,
    payload: dict[str, object],
    now: datetime,
    previous_hash: str | None,
    event_hash: str,
) -> RunEventRow:
    return RunEventRow(
        event_id=event_id,
        run_id=run_id,
        sequence=sequence,
        event_type="snapshot.completed",
        payload=payload,
        occurred_at=now,
        previous_hash=previous_hash,
        event_hash=event_hash,
    )


def _completion_outbox(
    outbox_id: UUID,
    run_id: UUID,
    sequence: int,
    payload: dict[str, object],
    job: JobRow,
    request: CompleteSnapshotJob,
    now: datetime,
) -> OutboxRow:
    return OutboxRow(
        outbox_id=outbox_id,
        aggregate_type="run",
        aggregate_id=str(run_id),
        sequence=sequence,
        topic="snapshot.completed",
        payload=payload,
        idempotency_key=f"job:{job.job_id}:complete:{request.attempt_generation}",
        created_at=now,
        not_before=now,
        attempts=0,
    )


def _mark_completed(
    job: JobRow,
    run: WorkflowRunRow,
    request: CompleteSnapshotJob,
    sequence: int,
    now: datetime,
) -> None:
    run.status = WorkflowStatus.SUCCEEDED.value
    run.version = sequence
    run.updated_at = now
    job.status = JobStatus.SUCCEEDED.value
    job.result_artifact_hash = request.snapshot.manifest_artifact_hash
    job.updated_at = now


def _completion_receipt(
    job: JobRow,
    run: WorkflowRunRow,
    request: CompleteSnapshotJob,
    event_id: UUID,
    outbox_id: UUID,
    sequence: int,
    now: datetime,
) -> SnapshotCompletionReceipt:
    snapshot = request.snapshot
    return SnapshotCompletionReceipt(
        job_id=job.job_id,
        run_id=run.run_id,
        event_id=event_id,
        outbox_id=outbox_id,
        sequence=sequence,
        result_artifact_hash=snapshot.manifest_artifact_hash,
        completed_at=now,
        snapshot_id=snapshot.snapshot_id,
        evidence_refs=snapshot.evidence_refs,
    )


def _event_payload(
    job: JobRow,
    request: CompleteSnapshotJob,
) -> dict[str, object]:
    snapshot = request.snapshot
    return {
        "job_id": str(job.job_id),
        "job_type": job.job_type,
        "result_artifact_hash": snapshot.manifest_artifact_hash,
        "attempt_generation": request.attempt_generation,
        "snapshot_id": str(snapshot.snapshot_id),
        "evidence_refs": [str(value) for value in snapshot.evidence_refs],
    }


def _event_id(request: CompleteSnapshotJob) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"stonks:job:{request.job_id}:event:{request.attempt_generation}",
    )


def _outbox_id(request: CompleteSnapshotJob) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"stonks:job:{request.job_id}:outbox:{request.attempt_generation}",
    )


def _require_same(
    existing: object,
    candidate: object,
    fields: tuple[str, ...],
) -> None:
    if any(getattr(existing, field) != getattr(candidate, field) for field in fields):
        raise _CanonicalConflict


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
