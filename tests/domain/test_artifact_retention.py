from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.domain.artifact_retention import (
    ArtifactGCDisposition,
    ArtifactGCItem,
    ArtifactGCRequest,
    ArtifactMaintenanceAction,
    ArtifactMaintenanceAuditEvent,
    ArtifactMaintenancePhase,
    ArtifactRestoreRequest,
    ArtifactRetentionMode,
    ArtifactRetentionRequest,
    ArtifactStorageState,
    EnableArtifactLegalHold,
    artifact_maintenance_command_hash,
    artifact_maintenance_result_hash,
)
from stonks_agent.domain.errors import Success

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)
HASH = "a" * 64
OPERATION_ID = UUID("81000000-0000-4000-8000-000000000001")
EVENT_ID = UUID("81000000-0000-4000-8000-000000000002")


def test_retention_request_only_extends_into_the_future() -> None:
    value = ArtifactRetentionRequest(
        operation_id=OPERATION_ID,
        content_hash=HASH,
        retain_until=NOW + timedelta(days=30),
        mode=ArtifactRetentionMode.COMPLIANCE,
        actor="system:retention",
        reason="regulatory_archive",
        requested_at=NOW,
    )

    assert value.retain_until > value.requested_at
    assert value.mode is ArtifactRetentionMode.COMPLIANCE

    with pytest.raises(ValidationError):
        ArtifactRetentionRequest.model_validate(
            {**value.model_dump(mode="python"), "retain_until": NOW}
        )


@pytest.mark.parametrize(
    "model",
    (
        lambda: EnableArtifactLegalHold(
            operation_id=OPERATION_ID,
            content_hash="../escape",
            actor="system:retention",
            reason="litigation",
            requested_at=NOW,
        ),
        lambda: ArtifactRestoreRequest(
            operation_id=OPERATION_ID,
            content_hash=HASH,
            actor="",
            reason="recovery",
            requested_at=NOW,
        ),
        lambda: ArtifactGCRequest(
            operation_id=OPERATION_ID,
            cutoff_at=NOW,
            max_candidates=100,
            actor="system:gc",
            reason="orphan_cleanup",
            requested_at=NOW,
        ),
    ),
)
def test_commands_reject_invalid_hash_actor_and_cutoff(model: object) -> None:
    with pytest.raises(ValidationError):
        model()  # type: ignore[operator]


def test_gc_request_is_bounded_and_uses_an_older_cutoff() -> None:
    value = ArtifactGCRequest(
        operation_id=OPERATION_ID,
        cutoff_at=NOW - timedelta(days=7),
        max_candidates=500,
        actor="system:gc",
        reason="orphan_cleanup",
        requested_at=NOW,
    )

    assert value.max_candidates == 500
    with pytest.raises(ValidationError):
        ArtifactGCRequest.model_validate(
            {**value.model_dump(mode="python"), "max_candidates": 10_001}
        )


def test_storage_state_cannot_claim_finalized_without_both_versions() -> None:
    with pytest.raises(ValidationError):
        ArtifactStorageState(
            content_hash=HASH,
            finalized=True,
            object_version_id="object-v1",
            manifest_version_id=None,
            retention_mode=ArtifactRetentionMode.GOVERNANCE,
            retain_until=NOW + timedelta(days=1),
            legal_hold=False,
            encryption="AES256",
            observed_at=NOW,
        )


def test_gc_item_does_not_serialize_opaque_version_id() -> None:
    item = ArtifactGCItem(
        content_hash=HASH,
        version_id="opaque-version",
        disposition=ArtifactGCDisposition.DELETED,
    )

    assert "opaque-version" not in repr(item)
    assert "version_id" not in item.model_dump(mode="json")


def test_audit_event_is_hash_bound_and_contains_no_storage_secret() -> None:
    event = ArtifactMaintenanceAuditEvent.create(
        event_id=EVENT_ID,
        operation_id=OPERATION_ID,
        action=ArtifactMaintenanceAction.ENABLE_LEGAL_HOLD,
        phase=ArtifactMaintenancePhase.REQUESTED,
        content_hash=HASH,
        actor="system:retention",
        reason="litigation",
        command_hash="c" * 64,
        result_hash=None,
        occurred_at=NOW,
        outcome=None,
        previous_event_hash=None,
    )

    assert event.event_hash == event.recalculate_hash()
    assert event.command_hash == "c" * 64
    assert "url" not in event.model_dump_json().lower()
    with pytest.raises(ValidationError):
        ArtifactMaintenanceAuditEvent.model_validate(
            {**event.model_dump(mode="python"), "event_hash": "b" * 64}
        )


def test_audit_hashes_bind_command_and_opaque_result_versions() -> None:
    command = ArtifactRetentionRequest(
        operation_id=OPERATION_ID,
        content_hash=HASH,
        retain_until=NOW + timedelta(days=30),
        mode=ArtifactRetentionMode.COMPLIANCE,
        actor="system:retention",
        reason="regulatory_archive",
        requested_at=NOW,
    )
    state = ArtifactStorageState(
        content_hash=HASH,
        finalized=True,
        object_version_id="opaque-object-version",
        manifest_version_id="opaque-manifest-version",
        retention_mode=ArtifactRetentionMode.COMPLIANCE,
        retain_until=NOW + timedelta(days=30),
        legal_hold=False,
        encryption="aws:kms",
        observed_at=NOW,
    )

    command_hash = artifact_maintenance_command_hash(command)
    result_hash = artifact_maintenance_result_hash(Success(state))

    assert len(command_hash) == len(result_hash) == 64
    assert command_hash != result_hash
    assert "opaque-object-version" not in state.model_dump_json()
