"""Verify immutable artifacts before asking the core to commit a snapshot."""

from __future__ import annotations

from datetime import datetime

from stonks_agent.application.data.materialize_snapshot import (
    verify_snapshot_artifacts,
)
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.domain.provider_policy import ProviderPolicy
from stonks_agent.domain.snapshot import (
    CompleteSnapshotJob,
    SnapshotCompletionReceipt,
)
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_agent.ports.snapshot_completion import SnapshotCompletionStore


def complete_snapshot(
    request: CompleteSnapshotJob,
    *,
    now: datetime,
    artifacts: ArtifactStore,
    completions: SnapshotCompletionStore,
    policy: ProviderPolicy,
) -> Result[SnapshotCompletionReceipt]:
    """Resolve only verified references; the completion store owns DB atomicity."""

    verified = verify_snapshot_artifacts(request.snapshot, artifacts)
    if isinstance(verified, Failure):
        return verified
    raw_artifact = artifacts.manifest(request.snapshot.raw_artifact_hash)
    if isinstance(raw_artifact, Failure):
        return raw_artifact
    manifest_artifact = artifacts.manifest(request.snapshot.manifest_artifact_hash)
    if isinstance(manifest_artifact, Failure):
        return manifest_artifact
    return completions.complete(
        request,
        now=now,
        raw_artifact=raw_artifact.value,
        manifest_artifact=manifest_artifact.value,
        policy=policy,
    )
