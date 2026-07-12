"""Validated snapshot ingestion request and reference-only response."""

from __future__ import annotations

import json
import math
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.dataset_snapshot import (
    CanonicalDatasetSnapshotManifest,
    MaterializedSnapshot,
    ReconciliationTrace,
)
from stonks_agent.domain.errors import ErrorCode
from stonks_agent.domain.job import JobCompletionReceipt, JobStatus
from stonks_agent.domain.provider_policy import (
    ProviderPolicy,
    ProviderRoute,
    ReconciliationOutcome,
)
from stonks_contracts.common import (
    NonEmptyString,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)

MAX_QUERY_BYTES = 32 * 1024
MAX_QUERY_DEPTH = 5


class CreateSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    capability: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    as_of: UTCDateTime
    query: dict[str, object]
    provider_policy_id: NonEmptyString
    idempotency_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$",
    )
    requested_at: UTCDateTime

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        _validate_json(self.query, depth=0)
        encoded = json.dumps(
            self.query,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_QUERY_BYTES:
            raise ValueError("snapshot query exceeds size limit")
        return self

    @property
    def input_hash(self) -> str:
        return stable_payload_hash(
            {
                "market": self.market,
                "capability": self.capability,
                "as_of": self.as_of.isoformat(),
                "query": self.query,
                "provider_policy_id": self.provider_policy_id,
            }
        )


class SnapshotJobRefs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    job_id: UUID
    snapshot_id: UUID | None = None
    evidence_refs: tuple[UUID, ...] = ()


class CompleteSnapshotJob(BaseModel):
    """Untrusted worker result fenced by the lease issued by the core."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    worker_id: NonEmptyString
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString
    snapshot: MaterializedSnapshot


class SnapshotCompletionReceipt(JobCompletionReceipt):
    snapshot_id: UUID
    evidence_refs: tuple[UUID, ...]


class SnapshotFailureStage(StrEnum):
    PROVIDER = "provider"
    MATERIALIZATION = "materialization"


class FailSnapshotJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    run_id: UUID
    worker_id: NonEmptyString
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString
    payload_hash: Sha256
    lease_until: UTCDateTime
    deadline_at: UTCDateTime
    stage: SnapshotFailureStage
    error_code: ErrorCode
    reconciliation_trace: ReconciliationTrace | None = None
    reconciliation_trace_hash: Sha256 | None = None

    @model_validator(mode="after")
    def validate_reconciliation_trace(self) -> Self:
        trace = self.reconciliation_trace
        if (trace is None) != (self.reconciliation_trace_hash is None):
            raise ValueError("failure reconciliation trace and hash must coexist")
        if trace is None:
            return self
        rejected = {
            ReconciliationOutcome.REJECTED_THRESHOLD_EXCEEDED,
            ReconciliationOutcome.REJECTED_METRIC_MISMATCH,
        }
        if (
            trace.decision not in rejected
            or stable_payload_hash(trace) != self.reconciliation_trace_hash
        ):
            raise ValueError("failure reconciliation trace is invalid")
        return self


class SnapshotAttemptFailureReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    run_id: UUID
    event_id: UUID
    outbox_id: UUID
    sequence: int = Field(ge=1)
    status: JobStatus
    recorded_at: UTCDateTime


def authorized_snapshot_route(
    request: CreateSnapshotRequest,
    policy: ProviderPolicy,
    *,
    provider: str,
    endpoint: str,
) -> ProviderRoute | None:
    """Resolve only an exact route from the immutable selected policy."""

    if not snapshot_request_is_authorized(request, policy):
        return None
    return next(
        (
            route
            for route in policy.routes
            if route.provider == provider and endpoint in route.endpoints
        ),
        None,
    )


def snapshot_request_is_authorized(
    request: CreateSnapshotRequest,
    policy: ProviderPolicy,
) -> bool:
    return (
        request.provider_policy_id == policy.policy_id
        and request.market == policy.market
        and request.capability == policy.capability
    )


def snapshot_manifest_is_authorized(
    request: CreateSnapshotRequest,
    manifest: CanonicalDatasetSnapshotManifest,
    policy: ProviderPolicy,
) -> bool:
    """Bind a completed manifest to the exact request and provider route."""

    return (
        authorized_snapshot_route(
            request,
            policy,
            provider=manifest.provider,
            endpoint=manifest.endpoint,
        )
        is not None
        and manifest.request_hash == request.input_hash
        and manifest.market == request.market
        and manifest.capability == request.capability
        and manifest.as_of == request.as_of
        and manifest.query == request.query
        and manifest.provider_policy_id == request.provider_policy_id
        and _manifest_trace_is_authorized(request, manifest, policy)
    )


def _manifest_trace_is_authorized(
    request: CreateSnapshotRequest,
    manifest: CanonicalDatasetSnapshotManifest,
    policy: ProviderPolicy,
) -> bool:
    trace = manifest.reconciliation_trace
    if trace is None:
        return True
    return reconciliation_trace_is_authorized(request, trace, policy)


def reconciliation_trace_is_authorized(
    request: CreateSnapshotRequest,
    trace: ReconciliationTrace,
    policy: ProviderPolicy,
) -> bool:
    """Revalidate a trace and bind both candidate routes to active policy."""

    try:
        canonical = ReconciliationTrace.model_validate(trace.model_dump(mode="python"))
    except (TypeError, ValueError):
        return False
    candidates = (canonical.primary, canonical.secondary)
    return (
        canonical == trace
        and canonical.policy_id == policy.policy_id
        and canonical.policy_threshold == policy.reconciliation_threshold
        and all(
            authorized_snapshot_route(
                request,
                policy,
                provider=candidate.provider,
                endpoint=candidate.endpoint,
            )
            is not None
            for candidate in candidates
        )
    )


def _validate_json(value: object, *, depth: int) -> None:
    if depth > MAX_QUERY_DEPTH:
        raise ValueError("snapshot query nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("snapshot query numbers must be finite")
        return
    if isinstance(value, list):
        if len(value) > 1000:
            raise ValueError("snapshot query list is too large")
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 256:
            raise ValueError("snapshot query object is too large")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ValueError("snapshot query keys are invalid")
            _validate_json(item, depth=depth + 1)
        return
    raise ValueError("snapshot query contains a non-JSON value")
