from __future__ import annotations

from decimal import Decimal

from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.usage_budget import (
    UsageBudget,
    UsageConsumption,
    consume_usage,
)


def budget() -> UsageBudget:
    return UsageBudget(
        max_iterations=4,
        max_tool_calls=6,
        max_input_tokens=1_000,
        max_output_tokens=500,
        max_total_tokens=1_200,
        max_cost_usd=Decimal("2.50"),
        max_elapsed_ms=30_000,
    )


def test_usage_is_accumulated_without_mutating_the_previous_snapshot() -> None:
    current = UsageConsumption()
    delta = UsageConsumption(
        iterations=1,
        tool_calls=2,
        input_tokens=200,
        output_tokens=50,
        cost_usd=Decimal("0.25"),
        elapsed_ms=500,
    )

    result = consume_usage(budget(), current, delta)

    assert isinstance(result, Success)
    assert result.value == delta
    assert current == UsageConsumption()


def test_each_budget_dimension_fails_closed_with_a_structured_error() -> None:
    overages = (
        UsageConsumption(iterations=5),
        UsageConsumption(tool_calls=7),
        UsageConsumption(input_tokens=1_001),
        UsageConsumption(output_tokens=501),
        UsageConsumption(input_tokens=800, output_tokens=401),
        UsageConsumption(cost_usd=Decimal("2.51")),
        UsageConsumption(elapsed_ms=30_001),
    )

    for usage in overages:
        result = consume_usage(budget(), UsageConsumption(), usage)
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.BUDGET_EXHAUSTED
        assert result.error.details["exceeded"]


def test_usage_budget_rejects_non_finite_or_negative_costs() -> None:
    for value in (Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")):
        try:
            UsageConsumption(cost_usd=value)
        except ValueError:
            pass
        else:  # pragma: no cover - security invariant
            raise AssertionError(f"unsafe cost accepted: {value}")


def test_accumulation_past_absolute_model_cap_returns_failure_not_exception() -> None:
    maximum = budget().model_copy(update={"max_iterations": 64})
    current = UsageConsumption(iterations=64)

    result = consume_usage(maximum, current, UsageConsumption(iterations=1))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.BUDGET_EXHAUSTED
