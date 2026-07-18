"""Artifact maintenance and durable audit boundaries."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.artifact_retention import (
    ArtifactGCReport,
    ArtifactGCRequest,
    ArtifactMaintenanceAuditEvent,
    ArtifactRestoreReceipt,
    ArtifactRestoreRequest,
    ArtifactRetentionRequest,
    ArtifactStorageState,
    EnableArtifactLegalHold,
)
from stonks_agent.domain.errors import Result


@runtime_checkable
class ArtifactMaintenanceBackendPort(Protocol):
    def extend_retention(
        self, request: ArtifactRetentionRequest
    ) -> Result[ArtifactStorageState]: ...

    def enable_legal_hold(
        self, request: EnableArtifactLegalHold
    ) -> Result[ArtifactStorageState]: ...

    def collect_orphans(
        self, request: ArtifactGCRequest
    ) -> Result[ArtifactGCReport]: ...

    def restore(
        self, request: ArtifactRestoreRequest
    ) -> Result[ArtifactRestoreReceipt]: ...


@runtime_checkable
class ArtifactMaintenanceAuditPort(Protocol):
    def record(
        self, event: ArtifactMaintenanceAuditEvent
    ) -> Result[ArtifactMaintenanceAuditEvent]: ...
