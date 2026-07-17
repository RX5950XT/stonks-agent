from __future__ import annotations

from decimal import Decimal

from stonks_agent.application.operational_budget import OperationalBudgetEvaluator
from stonks_agent.config.budgets import OperationalBudgetCatalog
from stonks_agent.domain.operational_budget import (
    BudgetDecision,
    BudgetScope,
    BudgetStatus,
    BudgetThreshold,
    BudgetUsage,
    OperationalBudgetPolicy,
)
from stonks_agent.domain.telemetry import BudgetDimension


def catalog() -> OperationalBudgetCatalog:
    return OperationalBudgetCatalog(
        schema_version=1,
        budgets=tuple(
            OperationalBudgetPolicy(
                scope=scope,
                degraded=BudgetThreshold(
                    action=BudgetStatus.DEGRADED,
                    max_cost_usd=Decimal("2"),
                    max_elapsed_seconds=Decimal("30"),
                ),
                failed=BudgetThreshold(
                    action=BudgetStatus.FAILED,
                    max_cost_usd=Decimal("5"),
                    max_elapsed_seconds=Decimal("60"),
                ),
            )
            for scope in BudgetScope
        ),
    )


class Usage:
    def __init__(self, value: object, *, explode: bool = False) -> None:
        self.value = value
        self.explode = explode
        self.calls: list[BudgetScope] = []

    def snapshot(self, scope: BudgetScope) -> object:
        self.calls.append(scope)
        if self.explode:
            raise RuntimeError("usage backend unavailable")
        return self.value


class BudgetMetrics:
    def __init__(self) -> None:
        self.calls: list[
            tuple[BudgetDimension, BudgetScope, BudgetStatus, float | None]
        ] = []

    def record_budget_evaluation(
        self,
        *,
        budget: BudgetDimension,
        scope: BudgetScope,
        outcome: BudgetStatus,
        usage_ratio: float | None,
    ) -> None:
        self.calls.append((budget, scope, outcome, usage_ratio))


def test_evaluator_uses_current_usage_and_emits_both_dimension_ratios() -> None:
    usage = Usage(
        BudgetUsage(
            cost_usd=Decimal("2.5"),
            monotonic_started_seconds=Decimal("100"),
            monotonic_observed_seconds=Decimal("110"),
        )
    )
    metrics = BudgetMetrics()
    evaluator = OperationalBudgetEvaluator(
        catalog=catalog(),
        usage=usage,
        metrics=metrics,
    )

    decision = evaluator.evaluate(BudgetScope.RESEARCH)

    assert decision.status is BudgetStatus.DEGRADED
    assert usage.calls == [BudgetScope.RESEARCH]
    assert metrics.calls == [
        (
            BudgetDimension.COST,
            BudgetScope.RESEARCH,
            BudgetStatus.DEGRADED,
            1.25,
        ),
        (
            BudgetDimension.LATENCY,
            BudgetScope.RESEARCH,
            BudgetStatus.DEGRADED,
            1 / 3,
        ),
    ]


def test_usage_provider_failure_is_a_closed_failed_decision() -> None:
    metrics = BudgetMetrics()
    evaluator = OperationalBudgetEvaluator(
        catalog=catalog(),
        usage=Usage(None, explode=True),
        metrics=metrics,
    )

    decision = evaluator.evaluate(BudgetScope.PAPER_CYCLE)

    assert decision.status is BudgetStatus.FAILED
    assert decision.usage is None
    assert all(call[3] is None for call in metrics.calls)


def test_previous_stronger_status_is_preserved_by_stateless_evaluator() -> None:
    evaluator = OperationalBudgetEvaluator(
        catalog=catalog(),
        usage=Usage(
            BudgetUsage(
                cost_usd=Decimal("0"),
                monotonic_started_seconds=Decimal("100"),
                monotonic_observed_seconds=Decimal("100"),
            )
        ),
    )

    decision: BudgetDecision = evaluator.evaluate(
        BudgetScope.RESEARCH,
        previous_status=BudgetStatus.FAILED,
    )

    assert decision.status is BudgetStatus.FAILED
    assert decision.evaluated_status is BudgetStatus.WITHIN
