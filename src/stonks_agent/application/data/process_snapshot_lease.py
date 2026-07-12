"""Typed vertical slice from a fenced lease to canonical snapshot completion."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from time import monotonic

from stonks_agent.application.data.complete_snapshot import complete_snapshot
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.application.data.materialize_snapshot import materialize_snapshot
from stonks_agent.domain.dataset_snapshot import (
    MaterializedSnapshot,
    ProviderSnapshotMaterialization,
    ReconciliationTrace,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
)
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.provider_policy import ProviderPolicy
from stonks_agent.domain.snapshot import (
    CompleteSnapshotJob,
    CreateSnapshotRequest,
    FailSnapshotJob,
    SnapshotCompletionReceipt,
    SnapshotFailureStage,
    authorized_snapshot_route,
)
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_agent.ports.snapshot_completion import SnapshotCompletionStore
from stonks_agent.ports.snapshot_materialization import (
    SnapshotMaterializationSource,
)
from stonks_contracts.common import stable_payload_hash


def process_snapshot_lease(
    lease: JobLease,
    *,
    now: datetime,
    source: SnapshotMaterializationSource[FetchDataRequest],
    artifacts: ArtifactStore,
    completions: SnapshotCompletionStore,
    policy: ProviderPolicy,
    monotonic_clock: Callable[[], float] = monotonic,
) -> Result[SnapshotCompletionReceipt]:
    """Process only the core-issued snapshot lease and preserve its fence."""

    started_at = monotonic_clock()
    preflight = completions.preflight(lease, now=now, policy=policy)
    if isinstance(preflight, Failure):
        return preflight
    request = preflight.value
    fetched = _fetch(source, request)
    current_time = _elapsed_now(now, started_at, monotonic_clock)
    if isinstance(fetched, Failure):
        return _record_failure(
            lease,
            fetched,
            SnapshotFailureStage.PROVIDER,
            current_time,
            policy,
            completions,
        )
    return _process_fetched(
        lease=lease,
        request=request,
        fetched=fetched.value,
        now=current_time,
        anchor_now=now,
        source_started_at=started_at,
        monotonic_clock=monotonic_clock,
        artifacts=artifacts,
        completions=completions,
        policy=policy,
    )


def _process_fetched(
    *,
    lease: JobLease,
    request: CreateSnapshotRequest,
    fetched: ProviderSnapshotMaterialization,
    now: datetime,
    anchor_now: datetime,
    source_started_at: float,
    monotonic_clock: Callable[[], float],
    artifacts: ArtifactStore,
    completions: SnapshotCompletionStore,
    policy: ProviderPolicy,
) -> Result[SnapshotCompletionReceipt]:
    refenced = completions.preflight(lease, now=now, policy=policy)
    if isinstance(refenced, Failure):
        return refenced
    if refenced.value != request:
        return _failure(ErrorCode.CONFLICT, "Snapshot preflight authority changed")
    denied = _route_failure(request, fetched, policy)
    if denied is not None:
        return _record_failure(
            lease, denied, SnapshotFailureStage.PROVIDER, now, policy, completions
        )
    snapshot = _materialize(request, fetched, artifacts)
    completed_at = _elapsed_now(anchor_now, source_started_at, monotonic_clock)
    if isinstance(snapshot, Failure):
        return _record_failure(
            lease,
            snapshot,
            SnapshotFailureStage.MATERIALIZATION,
            completed_at,
            policy,
            completions,
        )
    return _complete(
        lease,
        snapshot.value,
        completed_at,
        artifacts,
        completions,
        policy,
    )


def _fetch(
    source: SnapshotMaterializationSource[FetchDataRequest],
    request: CreateSnapshotRequest,
) -> Result[ProviderSnapshotMaterialization]:
    try:
        return source.fetch(
            FetchDataRequest(
                market=request.market,
                capability=request.capability,
                as_of=request.as_of,
                query=request.query,
            ),
            provider_policy_id=request.provider_policy_id,
        )
    except Exception:
        return _failure(ErrorCode.INTERNAL_ERROR, "Snapshot provider failed")


def _route_failure(
    request: CreateSnapshotRequest,
    fetched: ProviderSnapshotMaterialization,
    policy: ProviderPolicy,
) -> Failure | None:
    route = authorized_snapshot_route(
        request,
        policy,
        provider=fetched.provider,
        endpoint=fetched.endpoint,
    )
    if route is not None:
        return None
    return _failure(
        ErrorCode.CAPABILITY_DENIED,
        "Provider output is not authorized by snapshot policy",
    )


def _materialize(
    request: CreateSnapshotRequest,
    fetched: ProviderSnapshotMaterialization,
    artifacts: ArtifactStore,
) -> Result[MaterializedSnapshot]:
    try:
        return materialize_snapshot(request, fetched, artifacts)
    except Exception:
        return _failure(ErrorCode.INTERNAL_ERROR, "Snapshot materialization failed")


def _complete(
    lease: JobLease,
    snapshot: MaterializedSnapshot,
    now: datetime,
    artifacts: ArtifactStore,
    completions: SnapshotCompletionStore,
    policy: ProviderPolicy,
) -> Result[SnapshotCompletionReceipt]:
    return complete_snapshot(
        CompleteSnapshotJob(
            job_id=lease.job_id,
            worker_id=lease.lease_owner,
            attempt_generation=lease.attempt_generation,
            attempt_nonce=lease.attempt_nonce,
            snapshot=snapshot,
        ),
        now=now,
        artifacts=artifacts,
        completions=completions,
        policy=policy,
    )


def _record_failure(
    lease: JobLease,
    failure: Failure,
    stage: SnapshotFailureStage,
    now: datetime,
    policy: ProviderPolicy,
    completions: SnapshotCompletionStore,
) -> Failure:
    trace, trace_hash = _failure_trace(failure)
    recorded = completions.fail(
        FailSnapshotJob(
            job_id=lease.job_id,
            run_id=lease.run_id,
            worker_id=lease.lease_owner,
            attempt_generation=lease.attempt_generation,
            attempt_nonce=lease.attempt_nonce,
            payload_hash=stable_payload_hash(lease.payload),
            lease_until=lease.lease_until,
            deadline_at=lease.deadline_at,
            stage=stage,
            error_code=failure.error.code,
            reconciliation_trace=trace,
            reconciliation_trace_hash=trace_hash,
        ),
        now=now,
        policy=policy,
    )
    return recorded if isinstance(recorded, Failure) else failure


def _failure_trace(failure: Failure) -> tuple[ReconciliationTrace | None, str | None]:
    raw_trace = failure.error.details.get("reconciliation_trace")
    raw_hash = failure.error.details.get("reconciliation_trace_hash")
    if raw_trace is None or not isinstance(raw_hash, str):
        return None, None
    try:
        trace = ReconciliationTrace.model_validate(raw_trace)
    except (TypeError, ValueError):
        return None, None
    trace_hash = stable_payload_hash(trace)
    if trace_hash != raw_hash:
        return None, None
    return trace, trace_hash


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))


def _elapsed_now(
    anchor: datetime,
    started_at: float,
    clock: Callable[[], float],
) -> datetime:
    elapsed = max(clock() - started_at, 0.0)
    return anchor + timedelta(seconds=elapsed)
