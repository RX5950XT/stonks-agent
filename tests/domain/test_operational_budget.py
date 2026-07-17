from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from stonks_agent.domain.operational_budget import (
    BudgetDecision,
    BudgetDecisionReason,
    BudgetDimension,
    BudgetScope,
    BudgetStatus,
    BudgetThreshold,
    BudgetUsage,
    BudgetViolation,
    OperationalBudgetPolicy,
    evaluate_budget,
)


def policy() -> OperationalBudgetPolicy:
    return OperationalBudgetPolicy(
        scope=BudgetScope.RESEARCH,
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


def usage(
    *,
    cost_usd: str = "1",
    started: str = "100",
    observed: str = "110",
) -> BudgetUsage:
    return BudgetUsage(
        cost_usd=Decimal(cost_usd),
        monotonic_started_seconds=Decimal(started),
        monotonic_observed_seconds=Decimal(observed),
    )


def test_exact_limits_are_within_and_decision_is_immutable() -> None:
    decision = evaluate_budget(
        policy(),
        usage(cost_usd="2", observed="130"),
    )

    assert decision.status is BudgetStatus.WITHIN
    assert decision.evaluated_status is BudgetStatus.WITHIN
    assert decision.reason is BudgetDecisionReason.WITHIN_BUDGET
    assert decision.usage is not None
    assert decision.usage.elapsed_seconds == Decimal("30")
    assert decision.violations == ()
    with pytest.raises(ValidationError):
        decision.status = BudgetStatus.FAILED


@pytest.mark.parametrize(
    ("observed_usage", "expected_status", "expected_dimension"),
    (
        (usage(cost_usd="2.01"), BudgetStatus.DEGRADED, "cost"),
        (usage(observed="130.01"), BudgetStatus.DEGRADED, "latency"),
        (usage(cost_usd="5.01"), BudgetStatus.FAILED, "cost"),
        (usage(observed="160.01"), BudgetStatus.FAILED, "latency"),
    ),
)
def test_soft_and_hard_thresholds_produce_closed_outcomes(
    observed_usage: BudgetUsage,
    expected_status: BudgetStatus,
    expected_dimension: str,
) -> None:
    decision = evaluate_budget(policy(), observed_usage)

    assert decision.status is expected_status
    assert decision.evaluated_status is expected_status
    assert decision.reason is BudgetDecisionReason.THRESHOLD_EXCEEDED
    assert tuple(item.dimension.value for item in decision.violations) == (
        expected_dimension,
    )
    assert decision.violations[0].action is expected_status


def test_strongest_current_or_previous_outcome_is_preserved() -> None:
    degraded = evaluate_budget(policy(), usage(cost_usd="3"))
    preserved = evaluate_budget(
        policy(),
        usage(),
        previous_status=degraded.status,
    )
    failed = evaluate_budget(
        policy(),
        usage(observed="170"),
        previous_status=preserved.status,
    )
    still_failed = evaluate_budget(
        policy(),
        usage(),
        previous_status=failed.status,
    )

    assert tuple(
        decision.status for decision in (degraded, preserved, failed, still_failed)
    ) == (
        BudgetStatus.DEGRADED,
        BudgetStatus.DEGRADED,
        BudgetStatus.FAILED,
        BudgetStatus.FAILED,
    )
    assert preserved.evaluated_status is BudgetStatus.WITHIN
    assert still_failed.evaluated_status is BudgetStatus.WITHIN


@pytest.mark.parametrize(
    ("unsafe_usage", "reason"),
    (
        (None, BudgetDecisionReason.USAGE_MISSING),
        ({}, BudgetDecisionReason.USAGE_INVALID),
        (
            {
                "cost_usd": "1",
                "monotonic_started_seconds": "10",
                "monotonic_observed_seconds": "9",
            },
            BudgetDecisionReason.USAGE_INVALID,
        ),
        (
            {
                "cost_usd": "NaN",
                "monotonic_started_seconds": "10",
                "monotonic_observed_seconds": "11",
            },
            BudgetDecisionReason.USAGE_INVALID,
        ),
        (
            {
                "cost_usd": "1",
                "monotonic_started_seconds": "10",
                "monotonic_observed_seconds": "11",
                "unexpected": True,
            },
            BudgetDecisionReason.USAGE_INVALID,
        ),
    ),
)
def test_missing_or_invalid_usage_fails_closed(
    unsafe_usage: object,
    reason: BudgetDecisionReason,
) -> None:
    decision = evaluate_budget(policy(), unsafe_usage)

    assert decision.status is BudgetStatus.FAILED
    assert decision.evaluated_status is BudgetStatus.FAILED
    assert decision.reason is reason
    assert decision.usage is None
    assert decision.violations == ()


@pytest.mark.parametrize(
    "payload",
    (
        {
            "cost_usd": 1.0,
            "monotonic_started_seconds": "10",
            "monotonic_observed_seconds": "11",
        },
        {
            "cost_usd": "1000000.01",
            "monotonic_started_seconds": "10",
            "monotonic_observed_seconds": "11",
        },
        {
            "cost_usd": "1",
            "monotonic_started_seconds": "1000000000000.01",
            "monotonic_observed_seconds": "1000000000000.02",
        },
    ),
)
def test_usage_inputs_are_decimal_and_bounded(payload: object) -> None:
    with pytest.raises(ValidationError):
        BudgetUsage.model_validate(payload)


def test_policy_requires_ordered_soft_and_hard_limits() -> None:
    with pytest.raises(ValidationError):
        OperationalBudgetPolicy(
            scope=BudgetScope.RESEARCH,
            degraded=policy().degraded,
            failed=policy().failed.model_copy(update={"max_cost_usd": Decimal("2")}),
        )


def test_policy_rejects_non_actionable_or_swapped_threshold_actions() -> None:
    with pytest.raises(ValidationError):
        BudgetThreshold(
            action=BudgetStatus.WITHIN,
            max_cost_usd=Decimal("1"),
            max_elapsed_seconds=Decimal("1"),
        )
    with pytest.raises(ValidationError):
        OperationalBudgetPolicy(
            scope=BudgetScope.RESEARCH,
            degraded=policy().degraded.model_copy(
                update={"action": BudgetStatus.FAILED}
            ),
            failed=policy().failed,
        )
    with pytest.raises(ValidationError):
        OperationalBudgetPolicy(
            scope=BudgetScope.RESEARCH,
            degraded=policy().degraded,
            failed=policy().failed.model_copy(update={"action": BudgetStatus.DEGRADED}),
        )


def test_violation_contract_requires_a_real_actionable_overage() -> None:
    for action, observed in (
        (BudgetStatus.WITHIN, Decimal("3")),
        (BudgetStatus.DEGRADED, Decimal("2")),
    ):
        with pytest.raises(ValidationError):
            BudgetViolation(
                dimension=BudgetDimension.COST,
                action=action,
                threshold=Decimal("2"),
                observed=observed,
            )


def test_decision_contract_rejects_inconsistent_outcomes() -> None:
    with pytest.raises(ValidationError):
        BudgetDecision(
            scope=BudgetScope.RESEARCH,
            status=BudgetStatus.WITHIN,
            evaluated_status=BudgetStatus.FAILED,
            previous_status=BudgetStatus.WITHIN,
            reason=BudgetDecisionReason.USAGE_MISSING,
            usage=None,
            violations=(),
        )
    with pytest.raises(ValidationError):
        BudgetDecision(
            scope=BudgetScope.RESEARCH,
            status=BudgetStatus.WITHIN,
            evaluated_status=BudgetStatus.WITHIN,
            previous_status=BudgetStatus.WITHIN,
            reason=BudgetDecisionReason.USAGE_MISSING,
            usage=None,
            violations=(),
        )
    duplicate = BudgetViolation(
        dimension=BudgetDimension.COST,
        action=BudgetStatus.DEGRADED,
        threshold=Decimal("2"),
        observed=Decimal("3"),
    )
    with pytest.raises(ValidationError):
        BudgetDecision(
            scope=BudgetScope.RESEARCH,
            status=BudgetStatus.DEGRADED,
            evaluated_status=BudgetStatus.DEGRADED,
            previous_status=BudgetStatus.WITHIN,
            reason=BudgetDecisionReason.THRESHOLD_EXCEEDED,
            usage=usage(cost_usd="3"),
            violations=(duplicate, duplicate),
        )
    with pytest.raises(ValidationError):
        BudgetDecision(
            scope=BudgetScope.RESEARCH,
            status=BudgetStatus.WITHIN,
            evaluated_status=BudgetStatus.WITHIN,
            previous_status=BudgetStatus.WITHIN,
            reason=BudgetDecisionReason.THRESHOLD_EXCEEDED,
            usage=usage(),
            violations=(),
        )
