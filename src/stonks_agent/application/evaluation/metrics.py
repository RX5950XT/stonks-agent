"""Deterministic strategy performance metrics after explicit costs."""

from __future__ import annotations

from decimal import Decimal

from stonks_agent.application.evaluation.contracts import (
    EvaluationDataset,
    EvaluationPolicy,
    PerformanceMetrics,
    mean,
    position,
    quantize,
)
from stonks_agent.application.evaluation.costs import net_returns


def calculate_metrics(
    dataset: EvaluationDataset,
    policy: EvaluationPolicy,
    *,
    cost_multiplier: Decimal,
) -> PerformanceMetrics:
    gross = tuple(
        position(value.predicted_return) * value.actual_return
        for value in dataset.observations
    )
    net = net_returns(dataset, policy, cost_multiplier)
    benchmark = tuple(value.benchmark_return for value in dataset.observations)
    mean_net = quantize(mean(net))
    mean_benchmark = quantize(mean(benchmark))
    return PerformanceMetrics(
        observation_count=len(net),
        mean_gross_return=quantize(mean(gross)),
        mean_net_return=mean_net,
        mean_benchmark_return=mean_benchmark,
        net_alpha=quantize(mean_net - mean_benchmark),
        max_drawdown=quantize(_max_drawdown(net)),
        hit_rate=quantize(_hit_rate(dataset)),
        mean_turnover=quantize(
            mean(tuple(value.turnover for value in dataset.observations))
        ),
        sharpe_ratio=quantize(_sharpe(net)),
    )


def _max_drawdown(returns: tuple[Decimal, ...]) -> Decimal:
    equity = Decimal(1)
    peak = equity
    drawdown = Decimal(0)
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    return drawdown


def _hit_rate(dataset: EvaluationDataset) -> Decimal:
    hits = sum(
        1
        for value in dataset.observations
        if position(value.predicted_return) * value.actual_return > 0
    )
    return Decimal(hits) / Decimal(len(dataset.observations))


def _sharpe(returns: tuple[Decimal, ...]) -> Decimal:
    average = mean(returns)
    variance = mean(tuple((value - average) ** 2 for value in returns))
    if variance == 0:
        return Decimal(0)
    return average / variance.sqrt() * Decimal(252).sqrt()
