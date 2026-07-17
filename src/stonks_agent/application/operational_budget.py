"""Fail-closed composition for production cost and latency budgets."""

from __future__ import annotations

from contextlib import suppress
from decimal import Decimal

from stonks_agent.config.budgets import OperationalBudgetCatalog
from stonks_agent.domain.operational_budget import (
    BudgetDecision,
    BudgetScope,
    BudgetStatus,
    BudgetUsage,
    OperationalBudgetPolicy,
    evaluate_budget,
)
from stonks_agent.domain.telemetry import BudgetDimension
from stonks_agent.ports.operational_budget import BudgetMetricsPort, BudgetUsagePort


class OperationalBudgetEvaluator:
    """Evaluate untrusted usage snapshots against a closed policy catalog."""

    __slots__ = ("_catalog", "_metrics", "_usage")

    def __init__(
        self,
        *,
        catalog: OperationalBudgetCatalog,
        usage: BudgetUsagePort,
        metrics: BudgetMetricsPort | None = None,
    ) -> None:
        self._catalog = catalog
        self._usage = usage
        self._metrics = metrics

    def evaluate(
        self,
        scope: BudgetScope,
        *,
        previous_status: BudgetStatus = BudgetStatus.WITHIN,
    ) -> BudgetDecision:
        policy = self._catalog.policy_for(scope)
        try:
            usage = self._usage.snapshot(scope)
        except Exception:
            usage = None
        decision = evaluate_budget(
            policy,
            usage,
            previous_status=previous_status,
        )
        self._record(policy, decision)
        return decision

    def _record(
        self,
        policy: OperationalBudgetPolicy,
        decision: BudgetDecision,
    ) -> None:
        if self._metrics is None:
            return
        ratios = _usage_ratios(policy, decision.usage)
        for dimension in BudgetDimension:
            with suppress(Exception):
                self._metrics.record_budget_evaluation(
                    budget=dimension,
                    scope=decision.scope,
                    outcome=decision.status,
                    usage_ratio=ratios.get(dimension),
                )


def _usage_ratios(
    policy: OperationalBudgetPolicy,
    usage: BudgetUsage | None,
) -> dict[BudgetDimension, float]:
    if usage is None:
        return {}
    return {
        BudgetDimension.COST: _ratio(
            usage.cost_usd,
            policy.degraded.max_cost_usd,
        ),
        BudgetDimension.LATENCY: _ratio(
            usage.elapsed_seconds,
            policy.degraded.max_elapsed_seconds,
        ),
    }


def _ratio(value: Decimal, limit: Decimal) -> float:
    return float(value / limit)
