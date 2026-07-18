"""Audit-first artifact retention, GC, legal-hold, and restore use cases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from stonks_agent.domain.artifact_retention import (
    ArtifactGCReport,
    ArtifactGCRequest,
    ArtifactMaintenanceAction,
    ArtifactMaintenanceAuditEvent,
    ArtifactMaintenancePhase,
    ArtifactRestoreReceipt,
    ArtifactRestoreRequest,
    ArtifactRetentionRequest,
    ArtifactStorageState,
    EnableArtifactLegalHold,
    artifact_maintenance_command_hash,
    artifact_maintenance_result_hash,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, StructuredError
from stonks_agent.ports.artifact_maintenance import (
    ArtifactMaintenanceAuditPort,
    ArtifactMaintenanceBackendPort,
)

type MaintenanceCommand = (
    ArtifactRetentionRequest
    | EnableArtifactLegalHold
    | ArtifactGCRequest
    | ArtifactRestoreRequest
)
type MaintenanceResult = (
    ArtifactStorageState | ArtifactGCReport | ArtifactRestoreReceipt
)


def extend_artifact_retention(
    request: ArtifactRetentionRequest,
    *,
    backend: ArtifactMaintenanceBackendPort,
    audit: ArtifactMaintenanceAuditPort,
    event_id: Callable[[], UUID],
    clock: Callable[[], datetime],
) -> Result[ArtifactStorageState]:
    return _execute(
        request,
        action=ArtifactMaintenanceAction.EXTEND_RETENTION,
        operation=backend.extend_retention,
        audit=audit,
        event_id=event_id,
        clock=clock,
    )


def enable_artifact_legal_hold(
    request: EnableArtifactLegalHold,
    *,
    backend: ArtifactMaintenanceBackendPort,
    audit: ArtifactMaintenanceAuditPort,
    event_id: Callable[[], UUID],
    clock: Callable[[], datetime],
) -> Result[ArtifactStorageState]:
    return _execute(
        request,
        action=ArtifactMaintenanceAction.ENABLE_LEGAL_HOLD,
        operation=backend.enable_legal_hold,
        audit=audit,
        event_id=event_id,
        clock=clock,
    )


def run_artifact_gc(
    request: ArtifactGCRequest,
    *,
    backend: ArtifactMaintenanceBackendPort,
    audit: ArtifactMaintenanceAuditPort,
    event_id: Callable[[], UUID],
    clock: Callable[[], datetime],
) -> Result[ArtifactGCReport]:
    return _execute(
        request,
        action=ArtifactMaintenanceAction.COLLECT_ORPHANS,
        operation=backend.collect_orphans,
        audit=audit,
        event_id=event_id,
        clock=clock,
    )


def restore_artifact(
    request: ArtifactRestoreRequest,
    *,
    backend: ArtifactMaintenanceBackendPort,
    audit: ArtifactMaintenanceAuditPort,
    event_id: Callable[[], UUID],
    clock: Callable[[], datetime],
) -> Result[ArtifactRestoreReceipt]:
    return _execute(
        request,
        action=ArtifactMaintenanceAction.RESTORE,
        operation=backend.restore,
        audit=audit,
        event_id=event_id,
        clock=clock,
    )


def _execute[T: MaintenanceResult, C: MaintenanceCommand](
    request: C,
    *,
    action: ArtifactMaintenanceAction,
    operation: Callable[[C], Result[T]],
    audit: ArtifactMaintenanceAuditPort,
    event_id: Callable[[], UUID],
    clock: Callable[[], datetime],
) -> Result[T]:
    requested = _event(
        request,
        action=action,
        phase=ArtifactMaintenancePhase.REQUESTED,
        outcome=None,
        command_hash=artifact_maintenance_command_hash(request),
        result_hash=None,
        previous=None,
        event_id=event_id,
        clock=clock,
    )
    recorded = audit.record(requested)
    if isinstance(recorded, Failure):
        return recorded
    result = operation(request)
    phase, outcome = _outcome(result)
    terminal = _event(
        request,
        action=action,
        phase=phase,
        outcome=outcome,
        command_hash=requested.command_hash,
        result_hash=artifact_maintenance_result_hash(result),
        previous=recorded.value.event_hash,
        event_id=event_id,
        clock=clock,
    )
    terminal_recorded = audit.record(terminal)
    if isinstance(terminal_recorded, Failure):
        return _audit_failure()
    return result


def _event(
    request: MaintenanceCommand,
    *,
    action: ArtifactMaintenanceAction,
    phase: ArtifactMaintenancePhase,
    outcome: str | None,
    command_hash: str,
    result_hash: str | None,
    previous: str | None,
    event_id: Callable[[], UUID],
    clock: Callable[[], datetime],
) -> ArtifactMaintenanceAuditEvent:
    return ArtifactMaintenanceAuditEvent.create(
        event_id=event_id(),
        operation_id=request.operation_id,
        action=action,
        phase=phase,
        content_hash=getattr(request, "content_hash", None),
        actor=request.actor,
        reason=request.reason,
        command_hash=command_hash,
        result_hash=result_hash,
        occurred_at=clock(),
        outcome=outcome,
        previous_event_hash=previous,
    )


def _outcome(
    result: Result[MaintenanceResult],
) -> tuple[ArtifactMaintenancePhase, str]:
    if isinstance(result, Failure):
        return ArtifactMaintenancePhase.FAILED, result.error.code.value
    return ArtifactMaintenancePhase.COMPLETED, "success"


def _audit_failure() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.INTERNAL_ERROR,
            message="Artifact maintenance completion audit failed",
        )
    )
