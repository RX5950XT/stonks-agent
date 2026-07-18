"""Fail-closed artifact retention, GC, restore, and audit contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.errors import Failure, Result
from stonks_contracts.common import Sha256, UTCDateTime

_ACTOR_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{1,127}$"
_REASON_PATTERN = r"^[a-z][a-z0-9_.-]{1,127}$"


class ArtifactRetentionMode(StrEnum):
    GOVERNANCE = "governance"
    COMPLIANCE = "compliance"


class ArtifactEncryption(StrEnum):
    NONE = "none"
    AES256 = "AES256"
    KMS = "aws:kms"


class ArtifactMaintenanceAction(StrEnum):
    EXTEND_RETENTION = "extend_retention"
    ENABLE_LEGAL_HOLD = "enable_legal_hold"
    COLLECT_ORPHANS = "collect_orphans"
    RESTORE = "restore"


class ArtifactMaintenancePhase(StrEnum):
    REQUESTED = "requested"
    COMPLETED = "completed"
    FAILED = "failed"


class ArtifactGCDisposition(StrEnum):
    DELETED = "deleted"
    RETAINED_FINALIZED = "retained_finalized"
    RETAINED_TOO_NEW = "retained_too_new"
    RETAINED_LOCKED = "retained_locked"
    RETAINED_UNKNOWN = "retained_unknown"


class _ArtifactCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    actor: str = Field(min_length=2, max_length=128, pattern=_ACTOR_PATTERN)
    reason: str = Field(min_length=2, max_length=128, pattern=_REASON_PATTERN)
    requested_at: UTCDateTime


class ArtifactRetentionRequest(_ArtifactCommand):
    content_hash: Sha256
    retain_until: UTCDateTime
    mode: ArtifactRetentionMode

    @model_validator(mode="after")
    def validate_extension(self) -> Self:
        if self.retain_until <= self.requested_at:
            raise ValueError("retention must extend into the future")
        return self


class EnableArtifactLegalHold(_ArtifactCommand):
    content_hash: Sha256


class ArtifactRestoreRequest(_ArtifactCommand):
    content_hash: Sha256


class ArtifactGCRequest(_ArtifactCommand):
    cutoff_at: UTCDateTime
    max_candidates: int = Field(ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_cutoff(self) -> Self:
        if self.cutoff_at >= self.requested_at:
            raise ValueError("GC cutoff must precede request time")
        return self


class ArtifactStorageState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_hash: Sha256
    finalized: bool
    object_version_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=1_024,
        repr=False,
        exclude=True,
    )
    manifest_version_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=1_024,
        repr=False,
        exclude=True,
    )
    retention_mode: ArtifactRetentionMode | None = None
    retain_until: UTCDateTime | None = None
    legal_hold: bool
    encryption: ArtifactEncryption
    observed_at: UTCDateTime

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        versions = (
            self.object_version_id is not None,
            self.manifest_version_id is not None,
        )
        if self.finalized != all(versions):
            raise ValueError("finalized state requires both exact object versions")
        if (self.retention_mode is None) != (self.retain_until is None):
            raise ValueError("retention mode and timestamp must be present together")
        return self


class ArtifactGCItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_hash: Sha256
    version_id: str = Field(
        min_length=1,
        max_length=1_024,
        repr=False,
        exclude=True,
    )
    disposition: ArtifactGCDisposition


class ArtifactGCReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    cutoff_at: UTCDateTime
    scanned: int = Field(ge=0, le=10_000)
    items: tuple[ArtifactGCItem, ...] = Field(max_length=10_000)
    completed_at: UTCDateTime

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if len(self.items) > self.scanned:
            raise ValueError("GC report cannot contain more items than scanned")
        return self


class ArtifactRestoreReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    content_hash: Sha256
    removed_delete_markers: int = Field(ge=0, le=2)
    verified: bool
    completed_at: UTCDateTime

    @model_validator(mode="after")
    def validate_restore(self) -> Self:
        if not self.verified:
            raise ValueError("restore receipt must be verified")
        return self


class ArtifactMaintenanceAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    operation_id: UUID
    action: ArtifactMaintenanceAction
    phase: ArtifactMaintenancePhase
    content_hash: Sha256 | None
    actor: str = Field(min_length=2, max_length=128, pattern=_ACTOR_PATTERN)
    reason: str = Field(min_length=2, max_length=128, pattern=_REASON_PATTERN)
    command_hash: Sha256
    result_hash: Sha256 | None
    occurred_at: UTCDateTime
    outcome: str | None = Field(default=None, min_length=1, max_length=128)
    previous_event_hash: Sha256 | None
    event_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        event_id: UUID,
        operation_id: UUID,
        action: ArtifactMaintenanceAction,
        phase: ArtifactMaintenancePhase,
        content_hash: str | None,
        actor: str,
        reason: str,
        command_hash: str,
        result_hash: str | None,
        occurred_at: datetime,
        outcome: str | None,
        previous_event_hash: str | None,
    ) -> ArtifactMaintenanceAuditEvent:
        candidate = cls.model_construct(
            event_id=event_id,
            operation_id=operation_id,
            action=action,
            phase=phase,
            content_hash=content_hash,
            actor=actor,
            reason=reason,
            command_hash=command_hash,
            result_hash=result_hash,
            occurred_at=occurred_at,
            outcome=outcome,
            previous_event_hash=previous_event_hash,
            event_hash="0" * 64,
        )
        payload = candidate.model_dump(mode="json", exclude={"event_hash"})
        return cls.model_validate({**payload, "event_hash": _audit_hash(payload)})

    def recalculate_hash(self) -> str:
        return _audit_hash(self.model_dump(mode="json", exclude={"event_hash"}))

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.event_hash != self.recalculate_hash():
            raise ValueError("artifact maintenance audit hash is invalid")
        if (
            self.phase is ArtifactMaintenancePhase.REQUESTED
            and self.outcome is not None
        ):
            raise ValueError("requested audit event cannot have an outcome")
        if (
            self.phase is not ArtifactMaintenancePhase.REQUESTED
            and self.outcome is None
        ):
            raise ValueError("terminal audit event requires an outcome")
        if (self.phase is ArtifactMaintenancePhase.REQUESTED) != (
            self.result_hash is None
        ):
            raise ValueError("artifact audit result hash shape is invalid")
        return self


type ArtifactMaintenanceCommand = (
    ArtifactRetentionRequest
    | EnableArtifactLegalHold
    | ArtifactGCRequest
    | ArtifactRestoreRequest
)
type ArtifactMaintenanceResult = (
    ArtifactStorageState | ArtifactGCReport | ArtifactRestoreReceipt
)


def artifact_maintenance_command_hash(
    command: ArtifactMaintenanceCommand,
) -> str:
    return _audit_hash(command.model_dump(mode="json"))


def artifact_maintenance_result_hash(
    result: Result[ArtifactMaintenanceResult],
) -> str:
    if isinstance(result, Failure):
        return _audit_hash({"status": "failure", "error_code": result.error.code.value})
    value = result.value
    payload = value.model_dump(mode="json")
    payload["status"] = "success"
    if isinstance(value, ArtifactStorageState):
        payload["object_version_hash"] = _opaque_hash(value.object_version_id)
        payload["manifest_version_hash"] = _opaque_hash(value.manifest_version_id)
    elif isinstance(value, ArtifactGCReport):
        payload["version_hashes"] = tuple(
            _opaque_hash(item.version_id) for item in value.items
        )
    return _audit_hash(payload)


def _opaque_hash(value: str | None) -> str | None:
    return (
        hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None
    )


def _audit_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, UUID, StrEnum)):
        return str(value)
    raise TypeError("artifact audit payload is not JSON-compatible")
