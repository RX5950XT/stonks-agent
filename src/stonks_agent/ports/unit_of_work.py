"""Transactional repository boundary."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from stonks_agent.ports.evidence_repository import EvidenceRepository
from stonks_agent.ports.trading_repository import TradingRepositoryPort
from stonks_agent.ports.workflow_store import WorkflowStore


@runtime_checkable
class UnitOfWork(Protocol):
    evidence: EvidenceRepository
    workflows: WorkflowStore
    trading: TradingRepositoryPort

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
