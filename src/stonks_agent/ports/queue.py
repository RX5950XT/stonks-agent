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


@runtime_checkable
class QueuePort(Protocol):
    def enqueue(self, request: EnqueueJob) -> Result[JobRecord]: ...

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
    ) -> Result[JobCompletionReceipt]: ...
