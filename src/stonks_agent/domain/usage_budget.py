"""Immutable usage accounting and fail-closed research budgets."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_contracts.common import NonNegativeDecimal


class UsageBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(ge=1, le=64)
    max_tool_calls: int = Field(ge=0, le=256)
    max_input_tokens: int = Field(ge=0, le=10_000_000)
    max_output_tokens: int = Field(ge=0, le=1_000_000)
    max_total_tokens: int = Field(ge=0, le=10_000_000)
    max_cost_usd: NonNegativeDecimal
    max_elapsed_ms: int = Field(ge=1, le=86_400_000)


class UsageConsumption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    iterations: int = Field(default=0, ge=0, le=64)
    tool_calls: int = Field(default=0, ge=0, le=256)
    input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    output_tokens: int = Field(default=0, ge=0, le=1_000_000)
    cost_usd: NonNegativeDecimal = Decimal("0")
    elapsed_ms: int = Field(default=0, ge=0, le=86_400_000)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def consume_usage(
    budget: UsageBudget,
    current: UsageConsumption,
    delta: UsageConsumption,
) -> Result[UsageConsumption]:
    """Return a new usage snapshot or an explicit exhausted-budget failure."""

    iterations = current.iterations + delta.iterations
    tool_calls = current.tool_calls + delta.tool_calls
    input_tokens = current.input_tokens + delta.input_tokens
    output_tokens = current.output_tokens + delta.output_tokens
    cost_usd = current.cost_usd + delta.cost_usd
    elapsed_ms = current.elapsed_ms + delta.elapsed_ms
    exceeded = _exceeded_dimensions(
        budget,
        iterations=iterations,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        elapsed_ms=elapsed_ms,
    )
    if exceeded:
        return Failure(
            StructuredError(
                code=ErrorCode.BUDGET_EXHAUSTED,
                message="Research usage budget exhausted",
                details={"exceeded": exceeded},
            )
        )
    return Success(
        UsageConsumption(
            iterations=iterations,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            elapsed_ms=elapsed_ms,
        )
    )


def _exceeded_dimensions(
    budget: UsageBudget,
    *,
    iterations: int,
    tool_calls: int,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal,
    elapsed_ms: int,
) -> tuple[str, ...]:
    checks = (
        ("iterations", iterations > budget.max_iterations),
        ("tool_calls", tool_calls > budget.max_tool_calls),
        ("input_tokens", input_tokens > budget.max_input_tokens),
        ("output_tokens", output_tokens > budget.max_output_tokens),
        ("total_tokens", input_tokens + output_tokens > budget.max_total_tokens),
        ("cost_usd", cost_usd > budget.max_cost_usd),
        ("elapsed_ms", elapsed_ms > budget.max_elapsed_ms),
    )
    return tuple(name for name, exceeded in checks if exceeded)
