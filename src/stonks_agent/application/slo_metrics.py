"""Best-effort low-cardinality SLO and budget telemetry."""

from __future__ import annotations

from contextlib import suppress
from typing import Literal

from stonks_agent.domain.telemetry import (
    BudgetDimension,
    BudgetOutcome,
    BudgetScope,
    CorrectnessInvariant,
    MetricName,
)
from stonks_agent.ports.telemetry import MetricsPort

RuntimeEnvironment = Literal[
    "local",
    "development",
    "test",
    "staging",
    "production",
]


class SLOMetricsRecorder:
    """Emit policy metrics without gaining canonical outcome authority."""

    __slots__ = ("_environment", "_metrics")

    def __init__(
        self,
        *,
        metrics: MetricsPort,
        environment: RuntimeEnvironment,
    ) -> None:
        self._metrics = metrics
        self._environment = environment
        self._initialize_correctness_series()

    def record_correctness_violation(
        self,
        invariant: CorrectnessInvariant,
    ) -> None:
        with suppress(Exception):
            self._metrics.increment(
                MetricName.CORRECTNESS_VIOLATIONS,
                attributes={
                    "invariant": invariant,
                    "environment": self._environment,
                },
            )

    def record_budget_evaluation(
        self,
        *,
        budget: BudgetDimension,
        scope: BudgetScope,
        outcome: BudgetOutcome,
        usage_ratio: float | None,
    ) -> None:
        attributes = {
            "budget": budget,
            "scope": scope,
            "environment": self._environment,
        }
        if usage_ratio is not None:
            with suppress(Exception):
                self._metrics.observe(
                    MetricName.BUDGET_USAGE_RATIO,
                    usage_ratio,
                    attributes=attributes,
                )
        with suppress(Exception):
            self._metrics.increment(
                MetricName.BUDGET_OUTCOMES,
                attributes={**attributes, "outcome": outcome},
            )

    def _initialize_correctness_series(self) -> None:
        for invariant in CorrectnessInvariant:
            with suppress(Exception):
                self._metrics.increment(
                    MetricName.CORRECTNESS_VIOLATIONS,
                    0,
                    attributes={
                        "invariant": invariant,
                        "environment": self._environment,
                    },
                )
