"""Archive provider output and build a deterministic canonical snapshot."""

from __future__ import annotations

import hashlib
import hmac
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError

from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.data_quality import ProviderDataState
from stonks_agent.domain.dataset_snapshot import (
    CanonicalDatasetSnapshotManifest,
    MaterializedEvidence,
    MaterializedSnapshot,
    ProviderSnapshotMaterialization,
    SnapshotManifestEntry,
    validate_materialization_limits,
    validate_observation_evidence_link,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evidence import AvailabilityCertainty
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.evidence import Sensitivity

_ACCEPTED_STATES = frozenset(
    {
        ProviderDataState.AVAILABLE,
        ProviderDataState.LEGITIMATE_EMPTY,
        ProviderDataState.STALE,
        ProviderDataState.PARTIAL,
    }
)


def materialize_snapshot(
    request: CreateSnapshotRequest,
    materialization: ProviderSnapshotMaterialization,
    artifacts: ArtifactStore,
) -> Result[MaterializedSnapshot]:
    """Validate bounded provider output, then finalize canonical artifacts."""

    canonical = _canonical_input(materialization)
    if isinstance(canonical, Failure):
        return canonical
    materialization = canonical.value
    validation = _validate_materialization(request, materialization)
    if isinstance(validation, Failure):
        return validation
    raw_hash = hashlib.sha256(materialization.raw_payload).hexdigest()
    entries_result = _build_entries(request, materialization, raw_hash)
    if isinstance(entries_result, Failure):
        return entries_result
    manifest_result = _create_manifest(
        request,
        materialization,
        raw_hash,
        entries_result.value,
    )
    if isinstance(manifest_result, Failure):
        return manifest_result
    raw_result = _archive_raw(materialization, raw_hash, artifacts)
    if isinstance(raw_result, Failure):
        return raw_result
    return _archive_manifest(manifest_result.value, materialization, artifacts)


def _archive_raw(
    value: ProviderSnapshotMaterialization,
    expected_hash: str,
    artifacts: ArtifactStore,
) -> Result[str]:
    raw_result = artifacts.finalize(
        value.raw_payload,
        metadata=_raw_metadata(value),
        finalized_at=value.observation.observed_at,
    )
    if isinstance(raw_result, Failure):
        return raw_result
    if not hmac.compare_digest(raw_result.value.content_hash, expected_hash):
        return _failure(ErrorCode.CONFLICT, "Snapshot raw artifact hash mismatch")
    verified = _verify_bytes(
        artifacts, raw_result.value.content_hash, value.raw_payload
    )
    if isinstance(verified, Failure):
        return verified
    return Success(raw_result.value.content_hash)


def _canonical_input(
    value: ProviderSnapshotMaterialization,
) -> Result[ProviderSnapshotMaterialization]:
    try:
        validate_materialization_limits(value)
        payload = value.model_dump(mode="python", warnings=False)
        canonical = ProviderSnapshotMaterialization.model_validate(payload)
        validate_observation_evidence_link(canonical)
    except (AttributeError, RecursionError, TypeError, ValueError, ValidationError):
        return _failure(
            ErrorCode.INVALID_INPUT,
            "Snapshot provider output violates canonical input contract",
        )
    return Success(canonical)


def _create_manifest(
    request: CreateSnapshotRequest,
    value: ProviderSnapshotMaterialization,
    raw_hash: str,
    entries: tuple[SnapshotManifestEntry, ...],
) -> Result[CanonicalDatasetSnapshotManifest]:
    try:
        manifest = CanonicalDatasetSnapshotManifest(
            request_hash=request.request_hash,
            market=request.market,
            capability=request.capability,
            as_of=request.as_of,
            query=request.query,
            provider_policy_id=request.provider_policy_id,
            provider=value.provider,
            provider_version=value.provider_version,
            endpoint=value.endpoint,
            provider_state=value.observation.state,
            completeness=value.observation.completeness,
            reasons=tuple(sorted(set(value.observation.reasons))),
            provider_observed_at=value.observation.observed_at,
            raw_artifact_hash=raw_hash,
            raw_media_type=value.raw_media_type,
            license_tag=value.license_tag,
            redistribution_tag=value.redistribution_tag,
            sensitivity=value.sensitivity,
            reconciliation_trace=value.reconciliation_trace,
            entries=entries,
        )
    except (TypeError, ValueError, ValidationError):
        return _failure(ErrorCode.INVALID_INPUT, "Snapshot manifest is invalid")
    return Success(manifest)


def _archive_manifest(
    manifest: CanonicalDatasetSnapshotManifest,
    value: ProviderSnapshotMaterialization,
    artifacts: ArtifactStore,
) -> Result[MaterializedSnapshot]:
    manifest_bytes = manifest.canonical_bytes()
    manifest_result = artifacts.finalize(
        manifest_bytes,
        metadata=_manifest_metadata(value, manifest.raw_artifact_hash),
        finalized_at=value.observation.observed_at,
    )
    if isinstance(manifest_result, Failure):
        return manifest_result
    if not hmac.compare_digest(
        manifest_result.value.content_hash, manifest.content_hash
    ):
        return _failure(ErrorCode.CONFLICT, "Snapshot manifest artifact hash mismatch")
    manifest_verified = _verify_bytes(
        artifacts,
        manifest.content_hash,
        manifest_bytes,
    )
    if isinstance(manifest_verified, Failure):
        return manifest_verified
    return Success(
        MaterializedSnapshot(
            snapshot_id=manifest.snapshot_id,
            manifest_artifact_hash=manifest.content_hash,
            raw_artifact_hash=manifest.raw_artifact_hash,
            evidence_refs=tuple(item.evidence_id for item in manifest.entries),
            manifest=manifest,
        )
    )


def verify_snapshot_artifacts(
    snapshot: MaterializedSnapshot,
    artifacts: ArtifactStore,
) -> Result[bool]:
    """Fail closed unless both referenced artifacts and canonical JSON verify."""

    raw = artifacts.read(snapshot.raw_artifact_hash)
    if isinstance(raw, Failure):
        return raw
    if not _matches_hash(raw.value, snapshot.raw_artifact_hash):
        return _failure(ErrorCode.CONFLICT, "Snapshot raw artifact hash mismatch")

    stored_manifest = artifacts.read(snapshot.manifest_artifact_hash)
    if isinstance(stored_manifest, Failure):
        return stored_manifest
    if not _matches_hash(stored_manifest.value, snapshot.manifest_artifact_hash):
        return _failure(ErrorCode.CONFLICT, "Snapshot manifest artifact hash mismatch")
    try:
        parsed = CanonicalDatasetSnapshotManifest.model_validate_json(
            stored_manifest.value
        )
    except ValidationError:
        return _failure(ErrorCode.CONFLICT, "Snapshot manifest artifact is invalid")
    if parsed != snapshot.manifest or stored_manifest.value != parsed.canonical_bytes():
        return _failure(ErrorCode.CONFLICT, "Snapshot manifest is not canonical")
    return Success(True)


def _validate_materialization(
    request: CreateSnapshotRequest,
    value: ProviderSnapshotMaterialization,
) -> Result[bool]:
    state = value.observation.state
    if state not in _ACCEPTED_STATES:
        return _failure(
            ErrorCode.DATA_UNAVAILABLE,
            "Provider observation was not accepted for a snapshot",
        )
    if state is ProviderDataState.LEGITIMATE_EMPTY:
        if value.evidence:
            return _failure(
                ErrorCode.INVALID_INPUT,
                "Legitimate-empty provider output cannot contain evidence",
            )
        return Success(True)
    if not value.evidence:
        return _failure(
            ErrorCode.INVALID_INPUT,
            "Non-empty provider output requires materialized evidence",
        )
    for item in value.evidence:
        timeline = item.timeline
        if (
            not timeline.strict_point_in_time
            or timeline.availability_certainty is not AvailabilityCertainty.PROVEN
            or timeline.available_at > request.as_of
            or timeline.as_of > request.as_of
            or timeline.observed_at > value.observation.observed_at
        ):
            return _failure(
                ErrorCode.INVALID_INPUT,
                "Snapshot evidence is not proven point-in-time safe",
            )
    return Success(True)


def _build_entries(
    request: CreateSnapshotRequest,
    materialization: ProviderSnapshotMaterialization,
    raw_hash: str,
) -> Result[tuple[SnapshotManifestEntry, ...]]:
    try:
        entries = [
            _build_entry(request, materialization, item, raw_hash)
            for item in materialization.evidence
        ]
    except (TypeError, ValueError, ValidationError):
        return _failure(
            ErrorCode.INVALID_INPUT,
            "Materialized evidence payload is not canonical JSON",
        )
    ordered = tuple(
        sorted(
            entries,
            key=lambda item: (
                item.timeline.event_time,
                item.subject,
                item.kind,
                item.payload_hash,
                str(item.evidence_id),
            ),
        )
    )
    if len({item.evidence_id for item in ordered}) != len(ordered):
        return _failure(ErrorCode.CONFLICT, "Snapshot evidence is duplicated")
    return Success(ordered)


def _build_entry(
    request: CreateSnapshotRequest,
    materialization: ProviderSnapshotMaterialization,
    item: MaterializedEvidence,
    raw_hash: str,
) -> SnapshotManifestEntry:
    payload_hash = stable_payload_hash(item.payload)
    return SnapshotManifestEntry(
        evidence_id=_evidence_id(
            request=request,
            materialization=materialization,
            item=item,
            raw_hash=raw_hash,
            payload_hash=payload_hash,
        ),
        subject=item.subject,
        kind=item.kind,
        payload=item.payload,
        payload_hash=payload_hash,
        provider=materialization.provider,
        provider_version=materialization.provider_version,
        endpoint=materialization.endpoint,
        raw_artifact_hash=raw_hash,
        timeline=item.timeline,
        license_tag=materialization.license_tag,
        redistribution_tag=materialization.redistribution_tag,
        sensitivity=materialization.sensitivity,
    )


def _evidence_id(
    *,
    request: CreateSnapshotRequest,
    materialization: ProviderSnapshotMaterialization,
    item: MaterializedEvidence,
    raw_hash: str,
    payload_hash: str,
) -> UUID:
    identity_hash = stable_payload_hash(
        {
            "market": request.market,
            "capability": request.capability,
            "subject": item.subject,
            "kind": item.kind,
            "payload_hash": payload_hash,
            "timeline": item.timeline.model_dump(mode="json"),
            "provider": materialization.provider,
            "provider_version": materialization.provider_version,
            "endpoint": materialization.endpoint,
            "raw_artifact_hash": raw_hash,
            "license_tag": materialization.license_tag,
            "redistribution_tag": materialization.redistribution_tag,
        }
    )
    return uuid5(NAMESPACE_URL, f"stonks:evidence:{identity_hash}")


def _raw_metadata(value: ProviderSnapshotMaterialization) -> ArtifactMetadata:
    return ArtifactMetadata(
        media_type=value.raw_media_type,
        license_tag=value.license_tag,
        sensitivity=value.sensitivity,
        source=value.provider,
        attributes=(
            ("endpoint", value.endpoint),
            ("provider_version", value.provider_version),
            ("redistribution_tag", value.redistribution_tag),
        ),
    )


def _manifest_metadata(
    value: ProviderSnapshotMaterialization,
    raw_hash: str,
) -> ArtifactMetadata:
    return ArtifactMetadata(
        media_type="application/json",
        license_tag="Apache-2.0",
        sensitivity=Sensitivity.INTERNAL,
        source="stonks-agent",
        attributes=(
            ("provider", value.provider),
            ("raw_artifact_hash", raw_hash),
            ("schema", "canonical-dataset-snapshot/1.0.0"),
        ),
    )


def _verify_bytes(
    artifacts: ArtifactStore,
    content_hash: str,
    expected: bytes,
) -> Result[bool]:
    stored = artifacts.read(content_hash)
    if isinstance(stored, Failure):
        return stored
    if stored.value != expected or not _matches_hash(stored.value, content_hash):
        return _failure(ErrorCode.CONFLICT, "Finalized artifact failed verification")
    return Success(True)


def _matches_hash(content: bytes, expected_hash: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(content).hexdigest(), expected_hash)


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
