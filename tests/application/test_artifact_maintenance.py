from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from stonks_agent.application.artifact_maintenance import (
    enable_artifact_legal_hold,
    extend_artifact_retention,
    restore_artifact,
    run_artifact_gc,
)
from stonks_agent.domain.artifact_retention import (
    ArtifactGCReport,
    ArtifactGCRequest,
    ArtifactMaintenanceAuditEvent,
    ArtifactRestoreReceipt,
    ArtifactRestoreRequest,
    ArtifactRetentionMode,
    ArtifactRetentionRequest,
    ArtifactStorageState,
    EnableArtifactLegalHold,
)
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)
HASH = "a" * 64
OPERATION_ID = UUID("82000000-0000-4000-8000-000000000001")


class RecordingAudit:
    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.events: list[ArtifactMaintenanceAuditEvent] = []
        self._fail_on_call = fail_on_call

    def record(
        self, event: ArtifactMaintenanceAuditEvent
    ) -> Success[ArtifactMaintenanceAuditEvent] | Failure:
        self.events.append(event)
        if len(self.events) == self._fail_on_call:
            return Failure(
                StructuredError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="Artifact audit unavailable",
                )
            )
        return Success(event)


class RecordingBackend:
    def __init__(self, *, failure: Failure | None = None) -> None:
        self.failure = failure
        self.calls: list[object] = []

    def extend_retention(
        self, request: ArtifactRetentionRequest
    ) -> Success[ArtifactStorageState] | Failure:
        self.calls.append(request)
        return self.failure or Success(storage_state(legal_hold=False))

    def enable_legal_hold(
        self, request: EnableArtifactLegalHold
    ) -> Success[ArtifactStorageState] | Failure:
        self.calls.append(request)
        return self.failure or Success(storage_state(legal_hold=True))

    def collect_orphans(
        self, request: ArtifactGCRequest
    ) -> Success[ArtifactGCReport] | Failure:
        self.calls.append(request)
        return self.failure or Success(
            ArtifactGCReport(
                operation_id=request.operation_id,
                cutoff_at=request.cutoff_at,
                scanned=0,
                items=(),
                completed_at=NOW,
            )
        )

    def restore(
        self, request: ArtifactRestoreRequest
    ) -> Success[ArtifactRestoreReceipt] | Failure:
        self.calls.append(request)
        return self.failure or Success(
            ArtifactRestoreReceipt(
                operation_id=request.operation_id,
                content_hash=request.content_hash,
                removed_delete_markers=1,
                verified=True,
                completed_at=NOW,
            )
        )


def storage_state(*, legal_hold: bool) -> ArtifactStorageState:
    return ArtifactStorageState(
        content_hash=HASH,
        finalized=True,
        object_version_id="object-v1",
        manifest_version_id="manifest-v1",
        retention_mode=ArtifactRetentionMode.COMPLIANCE,
        retain_until=NOW + timedelta(days=30),
        legal_hold=legal_hold,
        encryption="AES256",
        observed_at=NOW,
    )


def retention_request() -> ArtifactRetentionRequest:
    return ArtifactRetentionRequest(
        operation_id=OPERATION_ID,
        content_hash=HASH,
        retain_until=NOW + timedelta(days=30),
        mode=ArtifactRetentionMode.COMPLIANCE,
        actor="system:retention",
        reason="regulatory_archive",
        requested_at=NOW,
    )


def test_mutation_records_requested_and_completed_events() -> None:
    backend, audit = RecordingBackend(), RecordingAudit()

    result = extend_artifact_retention(
        retention_request(),
        backend=backend,
        audit=audit,
        event_id=lambda: UUID(int=len(audit.events) + 1),
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    assert backend.calls == [retention_request()]
    assert [event.phase.value for event in audit.events] == ["requested", "completed"]
    assert audit.events[1].previous_event_hash == audit.events[0].event_hash
    assert audit.events[0].command_hash == audit.events[1].command_hash
    assert audit.events[0].result_hash is None
    assert audit.events[1].result_hash is not None
    assert "object-v1" not in audit.events[1].model_dump_json()


def test_preflight_audit_failure_prevents_external_mutation() -> None:
    backend, audit = RecordingBackend(), RecordingAudit(fail_on_call=1)

    result = enable_artifact_legal_hold(
        EnableArtifactLegalHold(
            operation_id=OPERATION_ID,
            content_hash=HASH,
            actor="system:retention",
            reason="litigation",
            requested_at=NOW,
        ),
        backend=backend,
        audit=audit,
        event_id=lambda: UUID(int=1),
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INTERNAL_ERROR
    assert backend.calls == []


def test_backend_failure_is_recorded_without_being_masked() -> None:
    unavailable = Failure(
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="Artifact backend unavailable",
        )
    )
    backend, audit = RecordingBackend(failure=unavailable), RecordingAudit()

    result = restore_artifact(
        ArtifactRestoreRequest(
            operation_id=OPERATION_ID,
            content_hash=HASH,
            actor="system:restore",
            reason="delete_marker_recovery",
            requested_at=NOW,
        ),
        backend=backend,
        audit=audit,
        event_id=lambda: UUID(int=len(audit.events) + 1),
        clock=lambda: NOW,
    )

    assert result is unavailable
    assert [event.phase.value for event in audit.events] == ["requested", "failed"]
    assert audit.events[1].outcome == ErrorCode.DATA_UNAVAILABLE.value


def test_completion_audit_failure_returns_fail_closed_error() -> None:
    backend, audit = RecordingBackend(), RecordingAudit(fail_on_call=2)

    result = run_artifact_gc(
        ArtifactGCRequest(
            operation_id=OPERATION_ID,
            cutoff_at=NOW - timedelta(days=7),
            max_candidates=100,
            actor="system:gc",
            reason="orphan_cleanup",
            requested_at=NOW,
        ),
        backend=backend,
        audit=audit,
        event_id=lambda: UUID(int=len(audit.events) + 1),
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INTERNAL_ERROR
    assert len(backend.calls) == 1
