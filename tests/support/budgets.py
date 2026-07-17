from __future__ import annotations

from decimal import Decimal

from stonks_agent.domain.operational_budget import (
    BudgetDecision,
    BudgetScope,
    BudgetStatus,
    BudgetThreshold,
    BudgetUsage,
    OperationalBudgetPolicy,
    evaluate_budget,
)


class FixedBudgetEvaluator:
    def __init__(
        self,
        statuses: tuple[BudgetStatus, ...] = (BudgetStatus.WITHIN,),
    ) -> None:
        self.statuses = statuses
        self.calls: list[BudgetScope] = []

    def evaluate(
        self,
        scope: BudgetScope,
        *,
        previous_status: BudgetStatus = BudgetStatus.WITHIN,
    ) -> BudgetDecision:
        index = min(len(self.calls), len(self.statuses) - 1)
        status = self.statuses[index]
        self.calls.append(scope)
        return evaluate_budget(
            _POLICIES[scope],
            _usage_for(status),
            previous_status=previous_status,
        )


_POLICIES = {
    scope: OperationalBudgetPolicy(
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
}


def _usage_for(status: BudgetStatus) -> BudgetUsage:
    cost = {
        BudgetStatus.WITHIN: "0",
        BudgetStatus.DEGRADED: "3",
        BudgetStatus.FAILED: "6",
    }[status]
    return BudgetUsage(
        cost_usd=Decimal(cost),
        monotonic_started_seconds=Decimal("100"),
        monotonic_observed_seconds=Decimal("100"),
    )
