"""Replayable workflow run and event contracts."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import Field

from .common import ArtifactRef, ContractModel, NonEmptyString, Sha256, UTCDateTime


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DEGRADED = "degraded"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Run(ContractModel):
    run_id: UUID
    state: RunState
    as_of: UTCDateTime
    created_at: UTCDateTime
    deadline: UTCDateTime
    policy_snapshot_ref: ArtifactRef
    config_snapshot_ref: ArtifactRef
    owner: str | None = None
    attempt: int = Field(default=0, ge=0)


class RunEvent(ContractModel):
    event_id: UUID
    run_id: UUID
    sequence: int = Field(ge=1)
    event_type: NonEmptyString
    payload_ref: ArtifactRef
    event_payload_hash: Sha256
    previous_event_hash: Sha256 | None = None
    causation_id: UUID | None = None
    producer: NonEmptyString
    occurred_at: UTCDateTime
