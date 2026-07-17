"""Runtime-checkable operational budget boundaries."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.operational_budget import (
    BudgetDecision,
    BudgetScope,
    BudgetStatus,
)
from stonks_agent.domain.telemetry import BudgetDimension


@runtime_checkable
class BudgetUsagePort(Protocol):
    def snapshot(self, scope: BudgetScope) -> object: ...


@runtime_checkable
class OperationalBudgetEvaluatorPort(Protocol):
    def evaluate(
        self,
        scope: BudgetScope,
        *,
        previous_status: BudgetStatus = BudgetStatus.WITHIN,
    ) -> BudgetDecision: ...


@runtime_checkable
class BudgetMetricsPort(Protocol):
    def record_budget_evaluation(
        self,
        *,
        budget: BudgetDimension,
        scope: BudgetScope,
        outcome: BudgetStatus,
        usage_ratio: float | None,
    ) -> None: ...
