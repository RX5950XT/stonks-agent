from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.application.data.policy_snapshot_source import (
    PolicySnapshotMaterializationSource,
)
from stonks_agent.application.data.process_snapshot_lease import process_snapshot_lease
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.dataset_snapshot import (
    MaterializedEvidence,
    ProviderSnapshotMaterialization,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.job import JobLease, JobStatus
from stonks_agent.domain.provider_policy import (
    ProviderPolicy,
    ProviderRoute,
    ReconciliationValue,
)
from stonks_agent.domain.snapshot import (
    CompleteSnapshotJob,
    CreateSnapshotRequest,
    FailSnapshotJob,
    SnapshotAttemptFailureReceipt,
    SnapshotCompletionReceipt,
)
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
JOB_ID = UUID("63000000-0000-4000-8000-000000000001")
RUN_ID = UUID("63000000-0000-4000-8000-000000000002")


class StaticSource:
    def __init__(self, value: ProviderSnapshotMaterialization) -> None:
        self.value = value
        self.calls = 0

    def fetch(
        self,
        request: FetchDataRequest,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        self.calls += 1
        return Success(self.value)


class CloseReconciliation:
    def extract(
        self,
        provider: str,
        observation: ProviderObservation[object],
    ) -> ReconciliationValue | None:
        payload = observation.data[-1] if observation.data else None
        if not isinstance(payload, dict) or not isinstance(payload.get("close"), str):
            return None
        return ReconciliationValue(
            metric="close",
            value=Decimal(payload["close"]),
        )


class CountingArtifactStore(MemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.finalize_calls = 0

    def finalize(
        self,
        content: object,
        *,
        metadata: object,
        finalized_at: object,
    ) -> Result[ArtifactManifest]:
        self.finalize_calls += 1
        return super().finalize(
            content,
            metadata=metadata,
            finalized_at=finalized_at,
        )


class RecordingCompletionStore:
    def __init__(self, request: CreateSnapshotRequest) -> None:
        self.request = request
        self.complete_calls = 0
        self.failures: list[FailSnapshotJob] = []

    def preflight(
        self,
        lease: JobLease,
        *,
        now: datetime,
        policy: ProviderPolicy,
    ) -> Result[CreateSnapshotRequest]:
        return Success(self.request)

    def complete(
        self,
        request: CompleteSnapshotJob,
        *,
        now: datetime,
        raw_artifact: ArtifactManifest,
        manifest_artifact: ArtifactManifest,
        policy: ProviderPolicy,
    ) -> Result[SnapshotCompletionReceipt]:
        self.complete_calls += 1
        raise AssertionError("conflicted candidates cannot complete a snapshot")

    def fail(
        self,
        request: FailSnapshotJob,
        *,
        now: datetime,
        policy: ProviderPolicy,
    ) -> Result[SnapshotAttemptFailureReceipt]:
        self.failures.append(request)
        return Success(
            SnapshotAttemptFailureReceipt(
                job_id=request.job_id,
                run_id=request.run_id,
                event_id=UUID("63000000-0000-4000-8000-000000000003"),
                outbox_id=UUID("63000000-0000-4000-8000-000000000004"),
                sequence=2,
                status=JobStatus.QUEUED,
                recorded_at=now,
            )
        )


def test_reconciliation_threshold_blocks_snapshot_artifact_and_canonical_write() -> (
    None
):
    request = snapshot_request()
    primary = StaticSource(materialization("primary", "100.00"))
    secondary = StaticSource(materialization("secondary", "103.00"))
    policy = provider_policy()
    source = PolicySnapshotMaterializationSource(
        policy=policy,
        sources={"primary": primary, "secondary": secondary},
        reconciliation_strategy=CloseReconciliation(),
    )
    artifacts = CountingArtifactStore()
    completions = RecordingCompletionStore(request)

    result = process_snapshot_lease(
        lease(request),
        now=NOW,
        source=source,
        artifacts=artifacts,
        completions=completions,
        policy=policy,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert result.error.details["reason"] == "reconciliation_threshold_exceeded"
    assert primary.calls == 1
    assert secondary.calls == 1
    assert artifacts.finalize_calls == 0
    assert completions.complete_calls == 0
    assert len(completions.failures) == 1
    assert completions.failures[0].stage == "provider"
    trace = completions.failures[0].reconciliation_trace
    assert trace is not None
    assert trace.decision == "rejected_threshold_exceeded"
    assert (
        trace.primary.raw_content_hash
        == result.error.details["reconciliation_trace"]["primary"]["raw_content_hash"]
    )
    assert completions.failures[0].reconciliation_trace_hash == stable_payload_hash(
        trace
    )


def snapshot_request() -> CreateSnapshotRequest:
    return CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL"},
        provider_policy_id="us-prices/1",
        idempotency_key="policy-reconciliation-e2e",
        owner_subject="test-owner",
        requested_at=NOW,
    )


def lease(request: CreateSnapshotRequest) -> JobLease:
    return JobLease(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type="create_snapshot",
        payload=request.model_dump(mode="json"),
        attempt_generation=1,
        attempt_nonce="nonce-a",
        lease_owner="worker-a",
        lease_until=NOW + timedelta(minutes=5),
        attempts=1,
        deadline_at=NOW + timedelta(minutes=15),
    )


def provider_policy() -> ProviderPolicy:
    return ProviderPolicy(
        policy_id="us-prices/1",
        market="US",
        capability="prices",
        routes=(provider_route("primary"), provider_route("secondary")),
        reconciliation_threshold=Decimal("0.01"),
    )


def provider_route(provider: str) -> ProviderRoute:
    return ProviderRoute(
        provider=provider,
        origin=f"https://{provider}.example",
        endpoints=("/v1/prices",),
        freshness_seconds=0,
        quota_floor=0,
    )


def materialization(provider: str, close: str) -> ProviderSnapshotMaterialization:
    timeline = EvidenceTimeline(
        event_time=NOW,
        published_at=NOW,
        available_at=NOW,
        observed_at=NOW,
        as_of=NOW,
        availability_certainty=AvailabilityCertainty.PROVEN,
    )
    return ProviderSnapshotMaterialization(
        provider=provider,
        provider_version="fixture/1",
        endpoint="/v1/prices",
        raw_payload=f'{{"provider":"{provider}","close":"{close}"}}'.encode(),
        raw_media_type="application/json",
        license_tag="CC0-1.0",
        redistribution_tag="synthetic-unrestricted",
        sensitivity=Sensitivity.PUBLIC,
        observation=ProviderObservation[object](
            state=ProviderDataState.AVAILABLE,
            data=({"close": close},),
            completeness=Decimal("1"),
            observed_at=NOW,
        ),
        evidence=(
            MaterializedEvidence(
                subject="AAPL",
                kind="market_data",
                payload={"close": close},
                timeline=timeline,
            ),
        ),
    )
