"""Durable PostgreSQL-backed queue boundary."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.job import (
    CompleteJob,
    EnqueueJob,
    JobCompletionReceipt,
    JobLease,
    JobRecord,
)
from stonks_agent.ports.artifact_store import ArtifactManifest


@runtime_checkable
class JobEnqueuePort(Protocol):
    def enqueue(self, request: EnqueueJob) -> Result[JobRecord]: ...


@runtime_checkable
class QueuePort(JobEnqueuePort, Protocol):
    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> Result[JobLease]: ...

    def complete(
        self,
        request: CompleteJob,
        *,
        now: datetime,
        artifact: ArtifactManifest | None = None,
    ) -> Result[JobCompletionReceipt]: ...
