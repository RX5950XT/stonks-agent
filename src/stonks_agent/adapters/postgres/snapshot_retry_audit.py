"""Full canonical graph verification for idempotent snapshot completion retry."""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select
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
from stonks_agent.domain.job import JobStatus
from stonks_agent.domain.provider_policy import ProviderPolicy
from stonks_agent.domain.snapshot import (
    CompleteSnapshotJob,
    CreateSnapshotRequest,
    SnapshotCompletionReceipt,
    snapshot_manifest_is_authorized,
)
from stonks_agent.domain.workflow import WorkflowStatus
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.market_data import DataQuality, DataQualityStatus


def completed_snapshot_receipt(
    session: Session,
    job: JobRow,
    request: CompleteSnapshotJob,
    *,
    raw_artifact: ArtifactManifest,
    manifest_artifact: ArtifactManifest,
    policy: ProviderPolicy,
) -> Result[SnapshotCompletionReceipt]:
    """Rebuild and verify every stable row before returning prior success."""

    queued = _validated_job(job, request)
    if isinstance(queued, Failure):
        return queued
    graph = _load_graph(session, job, request)
    if graph is None:
        return _failure("Completed snapshot graph is incomplete")
    run, event, outbox, snapshot, run_link, raw_row, manifest_row = graph
    if not _run_is_valid(run, event, job, queued.value, request, policy):
        return _failure("Completed snapshot run authority is invalid")
    if not _artifacts_are_valid(
        raw_row,
        manifest_row,
        raw_artifact,
        manifest_artifact,
        request.snapshot.manifest,
        event,
    ):
        return _failure("Completed snapshot artifacts are invalid")
    if not _canonical_rows_are_valid(session, snapshot, run_link, request, event):
        return _failure("Completed snapshot canonical rows are invalid")
    if not _audit_is_valid(session, job, event, outbox, request):
        return _failure("Completed snapshot audit is invalid")
    return Success(_receipt(job, event, outbox, request))


def _validated_job(
    job: JobRow,
    request: CompleteSnapshotJob,
) -> Result[CreateSnapshotRequest]:
    snapshot = request.snapshot
    same_result = (
        job.status == JobStatus.SUCCEEDED.value
        and job.job_type == "create_snapshot"
        and job.lease_owner == request.worker_id
        and job.attempt_generation == request.attempt_generation
        and job.attempt_nonce == request.attempt_nonce
        and job.result_artifact_hash == snapshot.manifest_artifact_hash
        and job.run_id is not None
        and stable_payload_hash(job.payload) == job.payload_hash
    )
    if not same_result:
        return _failure("Completed snapshot job is invalid")
    try:
        queued = CreateSnapshotRequest.model_validate(job.payload)
    except ValidationError:
        return _failure("Completed snapshot job payload is invalid")
    if job.payload != queued.model_dump(mode="json"):
        return _failure("Completed snapshot job payload is not canonical")
    return Success(queued)


type _Graph = tuple[
    WorkflowRunRow,
    RunEventRow,
    OutboxRow,
    DatasetSnapshotRow,
    RunDatasetSnapshotRow,
    ArtifactManifestRow,
    ArtifactManifestRow,
]


def _load_graph(
    session: Session,
    job: JobRow,
    request: CompleteSnapshotJob,
) -> _Graph | None:
    snapshot = request.snapshot
    values = (
        session.get(WorkflowRunRow, job.run_id),
        session.get(RunEventRow, _event_id(request)),
        session.get(OutboxRow, _outbox_id(request)),
        session.get(DatasetSnapshotRow, snapshot.snapshot_id),
        session.get(RunDatasetSnapshotRow, job.run_id),
        session.get(ArtifactManifestRow, snapshot.raw_artifact_hash),
        session.get(ArtifactManifestRow, snapshot.manifest_artifact_hash),
    )
    if any(value is None for value in values):
        return None
    run, event, outbox, stored, run_link, raw, manifest = values
    assert isinstance(run, WorkflowRunRow)
    assert isinstance(event, RunEventRow)
    assert isinstance(outbox, OutboxRow)
    assert isinstance(stored, DatasetSnapshotRow)
    assert isinstance(run_link, RunDatasetSnapshotRow)
    assert isinstance(raw, ArtifactManifestRow)
    assert isinstance(manifest, ArtifactManifestRow)
    return run, event, outbox, stored, run_link, raw, manifest


def _run_is_valid(
    run: WorkflowRunRow,
    event: RunEventRow,
    job: JobRow,
    queued: CreateSnapshotRequest,
    request: CompleteSnapshotJob,
    policy: ProviderPolicy,
) -> bool:
    manifest = request.snapshot.manifest
    return (
        run.run_id == job.run_id
        and run.run_type == "data_snapshot"
        and run.status == WorkflowStatus.SUCCEEDED.value
        and run.input_hash == queued.input_hash
        and run.as_of == queued.as_of
        and run.policy_id == queued.provider_policy_id
        and run.version == event.sequence
        and run.updated_at == event.occurred_at
        and snapshot_manifest_is_authorized(queued, manifest, policy)
        and manifest.provider_observed_at <= event.occurred_at
    )


def _artifacts_are_valid(
    raw_row: ArtifactManifestRow,
    manifest_row: ArtifactManifestRow,
    raw: ArtifactManifest,
    archived: ArtifactManifest,
    manifest: CanonicalDatasetSnapshotManifest,
    event: RunEventRow,
) -> bool:
    return (
        _artifact_row_matches(raw_row, raw)
        and _artifact_row_matches(manifest_row, archived)
        and raw.content_hash == manifest.raw_artifact_hash
        and raw.metadata.media_type == manifest.raw_media_type
        and raw.metadata.license_tag == manifest.license_tag
        and raw.metadata.sensitivity is manifest.sensitivity
        and raw.metadata.source == manifest.provider
        and raw.finalized_at == manifest.provider_observed_at
        and raw.finalized_at <= event.occurred_at
        and dict(raw.metadata.attributes) == _raw_attributes(manifest)
        and archived.content_hash == manifest.content_hash
        and archived.size_bytes == len(manifest.canonical_bytes())
        and archived.metadata.model_dump(mode="json") == _manifest_metadata(manifest)
        and archived.finalized_at == manifest.provider_observed_at
    )


def _artifact_row_matches(
    row: ArtifactManifestRow,
    artifact: ArtifactManifest,
) -> bool:
    return (
        row.content_hash == artifact.content_hash
        and row.size_bytes == artifact.size_bytes
        and row.media_type == artifact.metadata.media_type
        and row.license_tag == artifact.metadata.license_tag
        and row.sensitivity == artifact.metadata.sensitivity.value
        and row.source == artifact.metadata.source
        and row.finalized_at == artifact.finalized_at
        and row.storage_uri == artifact.storage_uri
        and row.metadata_payload == artifact.metadata.model_dump(mode="json")
    )


def _canonical_rows_are_valid(
    session: Session,
    stored: DatasetSnapshotRow,
    run_link: RunDatasetSnapshotRow,
    request: CompleteSnapshotJob,
    event: RunEventRow,
) -> bool:
    snapshot = request.snapshot
    linked = set(
        session.scalars(
            select(DatasetSnapshotEvidenceRow.evidence_id).where(
                DatasetSnapshotEvidenceRow.snapshot_id == snapshot.snapshot_id
            )
        ).all()
    )
    return (
        stored.snapshot_id == snapshot.snapshot_id
        and stored.as_of == snapshot.manifest.as_of
        and stored.cutoff_at == snapshot.manifest.as_of
        and stored.provider_policy_id == snapshot.manifest.provider_policy_id
        and stored.manifest_artifact_hash == snapshot.manifest_artifact_hash
        and stored.content_hash == snapshot.manifest.content_hash
        and stored.manifest == snapshot.manifest.model_dump(mode="json")
        and stored.created_at <= event.occurred_at
        and run_link.snapshot_id == snapshot.snapshot_id
        and run_link.created_at == event.occurred_at
        and linked == set(snapshot.evidence_refs)
        and len(linked) == len(snapshot.evidence_refs)
        and _evidence_rows_are_valid(session, snapshot.manifest, event)
    )


def _evidence_rows_are_valid(
    session: Session,
    manifest: CanonicalDatasetSnapshotManifest,
    event: RunEventRow,
) -> bool:
    rows = {
        row.evidence_id: row
        for row in session.scalars(
            select(EvidenceItemRow).where(
                EvidenceItemRow.evidence_id.in_(
                    entry.evidence_id for entry in manifest.entries
                )
            )
        ).all()
    }
    return len(rows) == len(manifest.entries) and all(
        _evidence_row_matches(rows.get(entry.evidence_id), entry, manifest, event)
        for entry in manifest.entries
    )


def _evidence_row_matches(
    row: EvidenceItemRow | None,
    entry: SnapshotManifestEntry,
    manifest: CanonicalDatasetSnapshotManifest,
    event: RunEventRow,
) -> bool:
    if row is None:
        return False
    quality = _quality(manifest)
    return (
        row.subject == entry.subject
        and row.kind == entry.kind
        and row.event_time == entry.timeline.event_time
        and row.published_at == entry.timeline.published_at
        and row.available_at == entry.timeline.available_at
        and row.observed_at == entry.timeline.observed_at
        and row.as_of == entry.timeline.as_of
        and row.availability_certainty == entry.timeline.availability_certainty.value
        and row.strict_point_in_time == entry.timeline.strict_point_in_time
        and row.source == entry.provider
        and row.provider == entry.provider
        and row.source_url is None
        and row.content_hash == stable_payload_hash(entry)
        and row.raw_artifact_hash == entry.raw_artifact_hash
        and row.quality_state == quality.status.value
        and row.quality == quality.model_dump(mode="json")
        and row.sensitivity == entry.sensitivity.value
        and row.license_tag == entry.license_tag
        and row.redistribution_tag == entry.redistribution_tag
        and row.payload == entry.payload
        and row.expires_at is None
        and row.transformation_version == entry.provider_version
        and row.untrusted_content is True
        and row.created_at <= event.occurred_at
    )


def _audit_is_valid(
    session: Session,
    job: JobRow,
    event: RunEventRow,
    outbox: OutboxRow,
    request: CompleteSnapshotJob,
) -> bool:
    payload = _event_payload(job, request)
    previous_hash = session.scalar(
        select(RunEventRow.event_hash)
        .where(
            RunEventRow.run_id == job.run_id,
            RunEventRow.sequence < event.sequence,
        )
        .order_by(RunEventRow.sequence.desc())
        .limit(1)
    )
    expected_hash = stable_payload_hash(
        {
            "event_id": str(event.event_id),
            "sequence": event.sequence,
            "previous_hash": previous_hash,
            "payload": payload,
        }
    )
    return (
        event.run_id == job.run_id
        and event.event_type == "snapshot.completed"
        and event.payload == payload
        and event.previous_hash == previous_hash
        and event.event_hash == expected_hash
        and outbox.aggregate_type == "run"
        and outbox.aggregate_id == str(job.run_id)
        and outbox.sequence == event.sequence
        and outbox.topic == "snapshot.completed"
        and outbox.payload == payload
        and outbox.idempotency_key
        == f"job:{job.job_id}:complete:{request.attempt_generation}"
        and outbox.created_at == event.occurred_at
        and outbox.not_before == event.occurred_at
    )


def _quality(manifest: CanonicalDatasetSnapshotManifest) -> DataQuality:
    statuses = {
        ProviderDataState.AVAILABLE: DataQualityStatus.AVAILABLE,
        ProviderDataState.STALE: DataQualityStatus.STALE,
        ProviderDataState.PARTIAL: DataQualityStatus.PARTIAL,
    }
    status = statuses.get(manifest.provider_state)
    if status is None:
        raise ValueError("snapshot provider state cannot contain evidence")
    return DataQuality(
        status=status,
        completeness=manifest.completeness,
        warnings=manifest.reasons,
    )


def _raw_attributes(
    manifest: CanonicalDatasetSnapshotManifest,
) -> dict[str, str]:
    return {
        "endpoint": manifest.endpoint,
        "provider_version": manifest.provider_version,
        "redistribution_tag": manifest.redistribution_tag,
    }


def _manifest_metadata(
    manifest: CanonicalDatasetSnapshotManifest,
) -> dict[str, object]:
    return {
        "media_type": "application/json",
        "license_tag": "Apache-2.0",
        "sensitivity": "internal",
        "source": "stonks-agent",
        "attributes": [
            ["provider", manifest.provider],
            ["raw_artifact_hash", manifest.raw_artifact_hash],
            ["schema", "canonical-dataset-snapshot/1.0.0"],
        ],
    }


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


def _receipt(
    job: JobRow,
    event: RunEventRow,
    outbox: OutboxRow,
    request: CompleteSnapshotJob,
) -> SnapshotCompletionReceipt:
    snapshot = request.snapshot
    assert job.run_id is not None
    return SnapshotCompletionReceipt(
        job_id=job.job_id,
        run_id=job.run_id,
        event_id=event.event_id,
        outbox_id=outbox.outbox_id,
        sequence=event.sequence,
        result_artifact_hash=snapshot.manifest_artifact_hash,
        completed_at=event.occurred_at,
        snapshot_id=snapshot.snapshot_id,
        evidence_refs=snapshot.evidence_refs,
    )


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


def _failure(message: str) -> Failure:
    return Failure(StructuredError(code=ErrorCode.CONFLICT, message=message))
