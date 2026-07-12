from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.data.process_snapshot_lease import process_snapshot_lease
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.dataset_snapshot import (
    MaterializedEvidence,
    ProviderSnapshotMaterialization,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.job import JobLease, JobStatus
from stonks_agent.domain.provider_policy import ProviderPolicy, ProviderRoute
from stonks_agent.domain.snapshot import (
    CompleteSnapshotJob,
    CreateSnapshotRequest,
    FailSnapshotJob,
    SnapshotAttemptFailureReceipt,
    SnapshotCompletionReceipt,
)
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
JOB_ID = UUID("62000000-0000-4000-8000-000000000001")
RUN_ID = UUID("62000000-0000-4000-8000-000000000002")


class StubSource:
    def __init__(self, result: Result[ProviderSnapshotMaterialization]) -> None:
        self.result = result
        self.calls = 0

    def fetch(
        self,
        request: object,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        self.calls += 1
        return self.result


class StubCompletionStore:
    def __init__(
        self,
        request: CreateSnapshotRequest,
        *,
        preflight_failure: Failure | None = None,
        preflight_failure_on_call: int | None = None,
        failure_transition: Failure | None = None,
    ) -> None:
        self.request = request
        self.preflight_failure = preflight_failure
        self.preflight_failure_on_call = preflight_failure_on_call
        self.failure_transition = failure_transition
        self.preflight_calls = 0
        self.preflight_times: list[datetime] = []
        self.failure_calls: list[FailSnapshotJob] = []
        self.completion_calls = 0

    def preflight(
        self,
        lease: JobLease,
        *,
        now: datetime,
        policy: ProviderPolicy,
    ) -> Result[CreateSnapshotRequest]:
        self.preflight_calls += 1
        self.preflight_times.append(now)
        if self.preflight_failure is not None and (
            self.preflight_failure_on_call is None
            or self.preflight_calls == self.preflight_failure_on_call
        ):
            return self.preflight_failure
        return Success(self.request)

    def fail(
        self,
        request: FailSnapshotJob,
        *,
        now: datetime,
        policy: ProviderPolicy,
    ) -> Result[SnapshotAttemptFailureReceipt]:
        self.failure_calls.append(request)
        if self.failure_transition is not None:
            return self.failure_transition
        return Success(
            SnapshotAttemptFailureReceipt(
                job_id=request.job_id,
                run_id=RUN_ID,
                event_id=UUID("62000000-0000-4000-8000-000000000003"),
                outbox_id=UUID("62000000-0000-4000-8000-000000000004"),
                sequence=2,
                status=JobStatus.QUEUED,
                recorded_at=now,
            )
        )

    def complete(
        self,
        request: CompleteSnapshotJob,
        *,
        now: datetime,
        raw_artifact: ArtifactManifest,
        manifest_artifact: ArtifactManifest,
        policy: ProviderPolicy,
    ) -> Result[SnapshotCompletionReceipt]:
        self.completion_calls += 1
        raise AssertionError("completion was not expected")


def test_db_preflight_failure_stops_before_provider_and_artifact_io() -> None:
    request = snapshot_request()
    source = StubSource(Success(materialization()))
    store = StubCompletionStore(
        request,
        preflight_failure=failure(ErrorCode.CONFLICT, "stale lease"),
    )
    artifacts = MemoryArtifactStore()

    result = process_snapshot_lease(
        lease(request),
        now=NOW,
        source=source,
        artifacts=artifacts,
        completions=store,
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert source.calls == 0
    assert store.failure_calls == []
    assert not artifacts.is_finalized(hashlib.sha256(b"raw").hexdigest())


def test_second_db_fence_blocks_artifacts_when_fetch_outlives_lease() -> None:
    request = snapshot_request()
    source = StubSource(Success(materialization()))
    store = StubCompletionStore(
        request,
        preflight_failure=failure(ErrorCode.CONFLICT, "lease expired"),
        preflight_failure_on_call=2,
    )
    artifacts = MemoryArtifactStore()
    ticks = iter((100.0, 106.0))

    result = process_snapshot_lease(
        lease(request).model_copy(update={"lease_until": NOW + timedelta(seconds=5)}),
        now=NOW,
        source=source,
        artifacts=artifacts,
        completions=store,
        policy=provider_policy(),
        monotonic_clock=lambda: next(ticks),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert store.preflight_times == [NOW, NOW + timedelta(seconds=6)]
    assert source.calls == 1
    assert store.failure_calls == []
    assert not artifacts.is_finalized(hashlib.sha256(b"raw").hexdigest())


@pytest.mark.parametrize(
    ("provider", "endpoint"),
    (("rogue", "/v1/prices"), ("replay", "/rogue")),
)
def test_rogue_materialization_is_rejected_before_any_artifact(
    provider: str,
    endpoint: str,
) -> None:
    request = snapshot_request()
    source = StubSource(Success(materialization(provider=provider, endpoint=endpoint)))
    store = StubCompletionStore(request)
    artifacts = MemoryArtifactStore()

    result = process_snapshot_lease(
        lease(request),
        now=NOW,
        source=source,
        artifacts=artifacts,
        completions=store,
        policy=provider_policy(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED
    assert not artifacts.is_finalized(hashlib.sha256(b"raw").hexdigest())
    assert len(store.failure_calls) == 1
    assert store.failure_calls[0].stage == "provider"
    assert store.completion_calls == 0


def test_provider_failure_is_fenced_and_transitions_out_of_leased() -> None:
    request = snapshot_request()
    source_failure = failure(ErrorCode.DATA_UNAVAILABLE, "provider unavailable")
    source = StubSource(source_failure)
    store = StubCompletionStore(request)

    result = process_snapshot_lease(
        lease(request),
        now=NOW,
        source=source,
        artifacts=MemoryArtifactStore(),
        completions=store,
        policy=provider_policy(),
    )

    assert result is source_failure
    assert len(store.failure_calls) == 1
    assert store.failure_calls[0].error_code is ErrorCode.DATA_UNAVAILABLE
    assert store.failure_calls[0].stage == "provider"


def test_stale_failure_fence_wins_over_provider_error() -> None:
    request = snapshot_request()
    source = StubSource(failure(ErrorCode.DATA_UNAVAILABLE, "provider unavailable"))
    stale = failure(ErrorCode.CONFLICT, "lease changed")
    store = StubCompletionStore(request, failure_transition=stale)

    result = process_snapshot_lease(
        lease(request),
        now=NOW,
        source=source,
        artifacts=MemoryArtifactStore(),
        completions=store,
        policy=provider_policy(),
    )

    assert result is stale
    assert len(store.failure_calls) == 1


def snapshot_request() -> CreateSnapshotRequest:
    return CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL"},
        provider_policy_id="us-prices/1",
        idempotency_key="process-unit",
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
        routes=(
            ProviderRoute(
                provider="replay",
                origin="https://replay.local",
                endpoints=("/v1/prices",),
                freshness_seconds=0,
                quota_floor=0,
            ),
        ),
        reconciliation_threshold=Decimal("0.01"),
    )


def materialization(
    *,
    provider: str = "replay",
    endpoint: str = "/v1/prices",
) -> ProviderSnapshotMaterialization:
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
        endpoint=endpoint,
        raw_payload=b"raw",
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


def failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
