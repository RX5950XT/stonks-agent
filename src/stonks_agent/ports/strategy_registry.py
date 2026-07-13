"""Typed strategy registry and transaction boundaries."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self, runtime_checkable
from uuid import UUID

from stonks_agent.domain.errors import Result
from stonks_agent.domain.evaluation import EvaluationReport
from stonks_agent.domain.strategy import (
    StrategyAuditEvent,
    StrategyMutationResult,
    StrategyRegistryEntry,
    StrategyTransitionRequest,
)


@runtime_checkable
class StrategyRegistryPort(Protocol):
    def get(
        self, strategy_id: str, strategy_version: str
    ) -> Result[StrategyRegistryEntry]: ...

    def get_evaluation(self, report_id: UUID) -> Result[EvaluationReport]: ...

    def list_events(
        self, strategy_id: str, strategy_version: str
    ) -> Result[tuple[StrategyAuditEvent, ...]]: ...

    def transition(
        self, request: StrategyTransitionRequest
    ) -> Result[StrategyMutationResult]: ...


@runtime_checkable
class StrategyUnitOfWork(Protocol):
    @property
    def strategies(self) -> StrategyRegistryPort: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@runtime_checkable
class StrategyUnitOfWorkFactory(Protocol):
    def __call__(self) -> StrategyUnitOfWork: ...
