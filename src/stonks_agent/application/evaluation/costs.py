"""Deterministic fees, slippage, turnover, and sensitivity scenarios."""

from __future__ import annotations

from decimal import Decimal

from stonks_agent.application.evaluation.contracts import (
    CostScenario,
    EvaluationDataset,
    EvaluationPolicy,
    mean,
    position,
    quantize,
)


def evaluate_cost_sensitivity(
    dataset: EvaluationDataset,
    policy: EvaluationPolicy,
) -> tuple[CostScenario, ...]:
    return tuple(
        _scenario(dataset, policy, multiplier) for multiplier in policy.cost_multipliers
    )


def net_returns(
    dataset: EvaluationDataset,
    policy: EvaluationPolicy,
    multiplier: Decimal,
) -> tuple[Decimal, ...]:
    rate = (policy.fee_bps + policy.slippage_bps) * multiplier / Decimal(10_000)
    return tuple(
        position(value.predicted_return) * value.actual_return - value.turnover * rate
        for value in dataset.observations
    )


def _scenario(
    dataset: EvaluationDataset,
    policy: EvaluationPolicy,
    multiplier: Decimal,
) -> CostScenario:
    returns = net_returns(dataset, policy, multiplier)
    gross = tuple(
        position(value.predicted_return) * value.actual_return
        for value in dataset.observations
    )
    total_cost = sum(gross, Decimal(0)) - sum(returns, Decimal(0))
    return CostScenario(
        multiplier=multiplier,
        mean_net_return=quantize(mean(returns)),
        total_cost=quantize(total_cost),
    )
