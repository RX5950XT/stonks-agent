"""Durable workflow state boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from stonks_agent.domain.errors import Result
from stonks_agent.domain.workflow import (
    CreateWorkflowRun,
    WorkflowRunRecord,
    WorkflowStatus,
)


@runtime_checkable
class WorkflowStore(Protocol):
    def create(self, request: CreateWorkflowRun) -> Result[WorkflowRunRecord]: ...

    def get(self, run_id: UUID) -> Result[WorkflowRunRecord]: ...

    def transition(
        self,
        run_id: UUID,
        *,
        expected_version: int,
        new_status: WorkflowStatus,
        updated_at: datetime,
    ) -> Result[WorkflowRunRecord]: ...
