"""Per-run monotonic cost tracking for operational budget decisions."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from threading import Lock
from time import monotonic

from stonks_agent.domain.errors import Failure, Result, Success
from stonks_agent.domain.operational_budget import BudgetScope, BudgetUsage
from stonks_agent.domain.research import (
    StructuredLLMRequest,
    StructuredLLMResponse,
)
from stonks_agent.domain.usage_budget import UsageConsumption
from stonks_agent.ports.llm import LLMPort


class MonotonicBudgetUsage:
    """One run-local accumulator using a single injected monotonic clock."""

    __slots__ = ("_clock", "_cost", "_lock", "_started")

    def __init__(
        self,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        started = _decimal_clock(monotonic_clock())
        self._clock = monotonic_clock
        self._started = started
        self._cost = Decimal(0)
        self._lock = Lock()

    def record(self, consumption: UsageConsumption) -> None:
        with self._lock:
            self._cost += consumption.cost_usd

    def snapshot(self, scope: BudgetScope) -> BudgetUsage:
        del scope
        observed = _decimal_clock(self._clock())
        with self._lock:
            cost = self._cost
        return BudgetUsage(
            cost_usd=cost,
            monotonic_started_seconds=self._started,
            monotonic_observed_seconds=observed,
        )


class UsageTrackingLLM:
    __slots__ = ("_delegate", "_usage")

    def __init__(self, delegate: LLMPort, usage: MonotonicBudgetUsage) -> None:
        self._delegate = delegate
        self._usage = usage

    def complete(
        self,
        request: StructuredLLMRequest,
    ) -> Result[StructuredLLMResponse]:
        result = self._delegate.complete(request)
        if isinstance(result, Success):
            self._usage.record(result.value.usage)
        elif isinstance(result, Failure):
            consumption = _failure_usage(result)
            if consumption is not None:
                self._usage.record(consumption)
        return result


def _failure_usage(failure: Failure) -> UsageConsumption | None:
    candidate = failure.error.details.get("usage")
    if not isinstance(candidate, dict):
        return None
    try:
        return UsageConsumption.model_validate(candidate)
    except ValueError:
        return None


def _decimal_clock(value: float) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("monotonic budget clock is invalid") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("monotonic budget clock is invalid")
    return parsed
