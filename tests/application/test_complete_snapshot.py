from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.data.complete_snapshot import complete_snapshot
from stonks_agent.application.data.materialize_snapshot import materialize_snapshot
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.dataset_snapshot import (
    MaterializedEvidence,
    MaterializedSnapshot,
    ProviderSnapshotMaterialization,
    ReconciliationCandidateTrace,
    ReconciliationTrace,
    normalized_evidence_content_hash,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.provider_policy import (
    ProviderPolicy,
    ProviderRoute,
    ReconciliationOutcome,
)
from stonks_agent.domain.snapshot import (
    CompleteSnapshotJob,
    CreateSnapshotRequest,
    FailSnapshotJob,
    SnapshotAttemptFailureReceipt,
    SnapshotCompletionReceipt,
    snapshot_manifest_is_authorized,
)
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_agent.ports.snapshot_completion import SnapshotCompletionStore
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
JOB_ID = UUID("61000000-0000-4000-8000-000000000001")


class RecordingCompletionStore:
    def __init__(self) -> None:
        self.calls: list[
            tuple[CompleteSnapshotJob, ArtifactManifest, ArtifactManifest]
        ] = []

    def complete(
        self,
        request: CompleteSnapshotJob,
        *,
        now: datetime,
        raw_artifact: ArtifactManifest,
        manifest_artifact: ArtifactManifest,
        policy: ProviderPolicy,
    ) -> Result[SnapshotCompletionReceipt]:
        self.calls.append((request, raw_artifact, manifest_artifact))
        return Success(
            SnapshotCompletionReceipt(
                job_id=request.job_id,
                run_id=UUID("61000000-0000-4000-8000-000000000002"),
                event_id=UUID("61000000-0000-4000-8000-000000000003"),
                outbox_id=UUID("61000000-0000-4000-8000-000000000004"),
                sequence=2,
                result_artifact_hash=request.snapshot.manifest_artifact_hash,
                completed_at=now,
                snapshot_id=request.snapshot.snapshot_id,
                evidence_refs=request.snapshot.evidence_refs,
            )
        )

    def preflight(
        self,
        lease: JobLease,
        *,
        now: datetime,
        policy: ProviderPolicy,
    ) -> Result[CreateSnapshotRequest]:
        raise AssertionError("preflight is not used by complete_snapshot")

    def fail(
        self,
        request: FailSnapshotJob,
        *,
        now: datetime,
        policy: ProviderPolicy,
    ) -> Result[SnapshotAttemptFailureReceipt]:
        raise AssertionError("failure transition is not used by complete_snapshot")


def test_completion_verifies_artifacts_before_calling_canonical_store() -> None:
    artifacts = MemoryArtifactStore()
    snapshot = unwrap(
        materialize_snapshot(snapshot_request(), materialization(), artifacts)
    )
    completion = RecordingCompletionStore()
    command = completion_command(snapshot)

    result = complete_snapshot(
        command,
        now=NOW,
        artifacts=artifacts,
        completions=completion,
        policy=provider_policy(),
    )

    assert isinstance(result, Success)
    assert len(completion.calls) == 1
    _, raw_manifest, snapshot_manifest = completion.calls[0]
    assert raw_manifest.content_hash == snapshot.raw_artifact_hash
    assert snapshot_manifest.content_hash == snapshot.manifest_artifact_hash


def test_missing_artifacts_fail_before_canonical_store_is_called() -> None:
    snapshot = unwrap(
        materialize_snapshot(
            snapshot_request(),
            materialization(),
            MemoryArtifactStore(),
        )
    )
    completion = RecordingCompletionStore()

    result = complete_snapshot(
        completion_command(snapshot),
        now=NOW,
        artifacts=MemoryArtifactStore(),
        completions=completion,
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.NOT_FOUND
    assert completion.calls == []


def test_completion_authority_rejects_an_internally_consistent_forged_threshold() -> (
    None
):
    value = materialization()
    forged_threshold = Decimal("0.02")
    trace = ReconciliationTrace(
        policy_id="us-prices/1",
        policy_threshold=forged_threshold,
        relative_difference=Decimal("1") / Decimal("101"),
        decision=ReconciliationOutcome.SELECTED_WITHIN_THRESHOLD,
        selected_provider="replay",
        primary=ReconciliationCandidateTrace(
            provider="replay",
            provider_version=value.provider_version,
            endpoint=value.endpoint,
            raw_content_hash=hashlib.sha256(value.raw_payload).hexdigest(),
            normalized_content_hash=normalized_evidence_content_hash(value.evidence),
            metric="close",
            value=Decimal("100"),
        ),
        secondary=ReconciliationCandidateTrace(
            provider="backup",
            provider_version="fixture/1",
            endpoint="/v1/prices",
            raw_content_hash="b" * 64,
            normalized_content_hash="c" * 64,
            metric="close",
            value=Decimal("101"),
        ),
    )
    traced = ProviderSnapshotMaterialization.model_validate(
        value.model_copy(update={"reconciliation_trace": trace}).model_dump(
            mode="python"
        )
    )
    snapshot = unwrap(
        materialize_snapshot(snapshot_request(), traced, MemoryArtifactStore())
    )

    assert snapshot.manifest.reconciliation_trace is not None
    assert snapshot.manifest.reconciliation_trace.policy_threshold == forged_threshold
    assert not snapshot_manifest_is_authorized(
        snapshot_request(), snapshot.manifest, provider_policy()
    )


def snapshot_request() -> CreateSnapshotRequest:
    return CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL"},
        provider_policy_id="us-prices/1",
        idempotency_key="completion-unit",
        requested_at=NOW,
    )


def materialization() -> ProviderSnapshotMaterialization:
    timeline = EvidenceTimeline(
        event_time=NOW,
        published_at=NOW,
        available_at=NOW,
        observed_at=NOW,
        as_of=NOW,
        availability_certainty=AvailabilityCertainty.PROVEN,
    )
    return ProviderSnapshotMaterialization(
        provider="replay",
        provider_version="fixture/1",
        endpoint="/v1/prices",
        raw_payload=b'{"symbol":"AAPL"}',
        raw_media_type="application/json",
        license_tag="CC0-1.0",
        redistribution_tag="synthetic-unrestricted",
        sensitivity=Sensitivity.PUBLIC,
        observation=ProviderObservation[object](
            state=ProviderDataState.AVAILABLE,
            data=({"close": "100.00"},),
            completeness=Decimal("1"),
            observed_at=NOW,
        ),
        evidence=(
            MaterializedEvidence(
                subject="AAPL",
                kind="market_data",
                payload={"close": "100.00"},
                timeline=timeline,
            ),
        ),
    )


def completion_command(snapshot: MaterializedSnapshot) -> CompleteSnapshotJob:
    return CompleteSnapshotJob(
        job_id=JOB_ID,
        worker_id="worker-a",
        attempt_generation=1,
        attempt_nonce="nonce-a",
        snapshot=snapshot,
    )


def provider_policy() -> ProviderPolicy:
    return ProviderPolicy(
        policy_id="us-prices/1",
        market="US",
        capability="prices",
        routes=(
            ProviderRoute(
                provider="replay",
                origin="https://replay.local",
                endpoints=("/v1/prices",),
                freshness_seconds=0,
                quota_floor=0,
            ),
            ProviderRoute(
                provider="backup",
                origin="https://backup.local",
                endpoints=("/v1/prices",),
                freshness_seconds=0,
                quota_floor=0,
            ),
        ),
        reconciliation_threshold=Decimal("0.01"),
    )


def unwrap[T](result: Result[T]) -> T:
    assert isinstance(result, Success)
    return result.value


assert isinstance(RecordingCompletionStore(), SnapshotCompletionStore)
