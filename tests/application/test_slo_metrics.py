from __future__ import annotations

from collections.abc import Mapping

from stonks_agent.application.slo_metrics import SLOMetricsRecorder
from stonks_agent.domain.telemetry import (
    BudgetDimension,
    BudgetOutcome,
    BudgetScope,
    CorrectnessInvariant,
    MetricName,
)


class RecordingMetrics:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.increments: list[tuple[str, int, dict[str, str]]] = []
        self.observations: list[tuple[str, float, dict[str, str]]] = []

    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("collector unavailable")
        self.increments.append((name, value, dict(attributes or {})))

    def observe(
        self,
        name: str,
        value: float,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("collector unavailable")
        self.observations.append((name, value, dict(attributes or {})))


def test_records_exact_low_cardinality_correctness_metric() -> None:
    metrics = RecordingMetrics()
    recorder = SLOMetricsRecorder(metrics=metrics, environment="production")

    assert metrics.increments == [
        (
            MetricName.CORRECTNESS_VIOLATIONS,
            0,
            {
                "invariant": invariant,
                "environment": "production",
            },
        )
        for invariant in (
            "duplicate_paper_order",
            "future_evidence",
            "claim_provenance",
            "risk_replayability",
        )
    ]
    recorder.record_correctness_violation(CorrectnessInvariant.FUTURE_EVIDENCE)

    assert metrics.increments[-1:] == [
        (
            MetricName.CORRECTNESS_VIOLATIONS,
            1,
            {
                "invariant": "future_evidence",
                "environment": "production",
            },
        )
    ]


def test_records_budget_ratio_and_outcome_without_identity_labels() -> None:
    metrics = RecordingMetrics()
    recorder = SLOMetricsRecorder(metrics=metrics, environment="staging")
    metrics.increments.clear()

    recorder.record_budget_evaluation(
        budget=BudgetDimension.COST,
        scope=BudgetScope.RESEARCH,
        outcome=BudgetOutcome.DEGRADED,
        usage_ratio=0.85,
    )

    assert metrics.observations == [
        (
            MetricName.BUDGET_USAGE_RATIO,
            0.85,
            {
                "budget": "cost",
                "scope": "research",
                "environment": "staging",
            },
        )
    ]
    assert metrics.increments == [
        (
            MetricName.BUDGET_OUTCOMES,
            1,
            {
                "budget": "cost",
                "scope": "research",
                "outcome": "degraded",
                "environment": "staging",
            },
        )
    ]


def test_metrics_failure_is_best_effort() -> None:
    recorder = SLOMetricsRecorder(
        metrics=RecordingMetrics(fail=True),
        environment="test",
    )

    recorder.record_correctness_violation(CorrectnessInvariant.DUPLICATE_PAPER_ORDER)
    recorder.record_budget_evaluation(
        budget=BudgetDimension.LATENCY,
        scope=BudgetScope.PAPER_CYCLE,
        outcome=BudgetOutcome.FAILED,
        usage_ratio=1.2,
    )
