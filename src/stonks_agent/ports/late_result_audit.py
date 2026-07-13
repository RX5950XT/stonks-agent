"""Isolated audit sink for worker results rejected by canonical fencing."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.job import QuarantinedWorkerResult


@runtime_checkable
class LateResultAuditPort(Protocol):
    def record(
        self, result: QuarantinedWorkerResult
    ) -> Result[QuarantinedWorkerResult]: ...
