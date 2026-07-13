from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from stonks_agent.application.evaluation.calibration import evaluate_calibration
from stonks_agent.application.evaluation.contracts import load_evaluation_policy
from stonks_agent.application.evaluation.costs import evaluate_cost_sensitivity
from stonks_agent.application.evaluation.metrics import calculate_metrics
from stonks_contracts.common import ConfidenceCalibration

from .helpers import dataset, policy

ROOT = Path(__file__).resolve().parents[2]


def test_cost_sensitivity_is_monotonic_and_includes_policy_multiplier() -> None:
    scenarios = evaluate_cost_sensitivity(dataset(), policy())

    assert tuple(value.multiplier for value in scenarios) == (
        Decimal("0.5"),
        Decimal("1"),
        Decimal("2"),
    )
    assert scenarios[0].mean_net_return >= scenarios[1].mean_net_return
    assert scenarios[1].mean_net_return >= scenarios[2].mean_net_return


def test_metrics_include_net_alpha_drawdown_hit_rate_and_turnover() -> None:
    value = calculate_metrics(dataset(), policy(), cost_multiplier=Decimal("1"))

    assert value.observation_count == 24
    assert value.mean_net_return > value.mean_benchmark_return
    assert value.net_alpha == value.mean_net_return - value.mean_benchmark_return
    assert Decimal("-1") <= value.max_drawdown <= 0
    assert 0 <= value.hit_rate <= 1
    assert value.mean_turnover == Decimal("0.250000000000")


def test_calibration_is_deterministic_and_threshold_gated() -> None:
    first = evaluate_calibration(dataset(), policy())
    second = evaluate_calibration(dataset(), policy())

    assert first == second
    assert first.status is ConfidenceCalibration.CALIBRATED
    assert first.brier_score <= policy().maximum_brier_score
    assert first.expected_calibration_error <= policy().maximum_calibration_error
    assert sum(bucket.count for bucket in first.buckets) == 24


def test_versioned_evaluation_policy_has_stable_content_hash() -> None:
    path = ROOT / "config" / "policies" / "evaluation_v1.yaml"

    first = load_evaluation_policy(path)
    second = load_evaluation_policy(path)

    assert first == second
    assert first.policy_hash == second.policy_hash
    assert first.cost_multipliers == (
        Decimal("0.5"),
        Decimal("1"),
        Decimal("2"),
    )
