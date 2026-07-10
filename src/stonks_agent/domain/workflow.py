"""Durable workflow run state and compare-and-swap inputs."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from stonks_contracts.common import NonEmptyString, Sha256, UTCDateTime


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DEGRADED = "degraded"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CreateWorkflowRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    run_type: NonEmptyString
    as_of: UTCDateTime
    policy_id: NonEmptyString
    idempotency_key: NonEmptyString
    input_hash: Sha256
    created_at: UTCDateTime


class WorkflowRunRecord(CreateWorkflowRun):
    status: WorkflowStatus
    version: int = Field(ge=1)
    updated_at: UTCDateTime


_ALLOWED_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.PENDING: frozenset(
        {WorkflowStatus.RUNNING, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.RUNNING: frozenset(
        {
            WorkflowStatus.DEGRADED,
            WorkflowStatus.SUCCEEDED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }
    ),
    WorkflowStatus.DEGRADED: frozenset(
        {WorkflowStatus.SUCCEEDED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.SUCCEEDED: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
    WorkflowStatus.CANCELLED: frozenset(),
}


def can_transition(current: WorkflowStatus, target: WorkflowStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]
