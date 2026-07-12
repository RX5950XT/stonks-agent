"""Core-owned canonical snapshot completion boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.provider_policy import ProviderPolicy
from stonks_agent.domain.snapshot import (
    CompleteSnapshotJob,
    CreateSnapshotRequest,
    FailSnapshotJob,
    SnapshotAttemptFailureReceipt,
    SnapshotCompletionReceipt,
)
from stonks_agent.ports.artifact_store import ArtifactManifest


@runtime_checkable
class SnapshotCompletionStore(Protocol):
    def preflight(
        self,
        lease: JobLease,
        *,
        now: datetime,
        policy: ProviderPolicy,
    ) -> Result[CreateSnapshotRequest]: ...

    def complete(
        self,
        request: CompleteSnapshotJob,
        *,
        now: datetime,
        raw_artifact: ArtifactManifest,
        manifest_artifact: ArtifactManifest,
        policy: ProviderPolicy,
    ) -> Result[SnapshotCompletionReceipt]: ...

    def fail(
        self,
        request: FailSnapshotJob,
        *,
        now: datetime,
        policy: ProviderPolicy,
    ) -> Result[SnapshotAttemptFailureReceipt]: ...
