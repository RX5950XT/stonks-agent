from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.application.evaluation.promotion import evaluate_for_promotion
from stonks_agent.domain.errors import Failure, Success
from stonks_agent.domain.evaluation import MANDATORY_EVALUATION_CHECKS
from stonks_contracts.common import ConfidenceCalibration

from .helpers import HASH_D, NOW, baselines, dataset, policy, request


def test_full_evaluation_builds_passed_exactly_bound_report() -> None:
    result = evaluate_for_promotion(
        request=request(),
        dataset=dataset(),
        baselines=baselines(),
        policy=policy(),
        report_id=UUID("00000000-0000-4000-8000-000000000505"),
        report_artifact_ref=f"sha256:{HASH_D}",
        created_at=NOW,
    )

    assert isinstance(result, Success)
    report = result.value
    assert report.passed is True
    assert report.calibration is ConfidenceCalibration.CALIBRATED
    assert {check.kind for check in report.checks} >= MANDATORY_EVALUATION_CHECKS
    assert report.strategy_manifest_hash == request().manifest.manifest_hash
    assert report.dataset_snapshot_id == request().dataset_snapshot_id
    assert report.runtime_hash == request().runtime_hash
    oos = next(
        value for value in report.metrics if value.name == "out_of_sample_observations"
    )
    assert oos.value == 12


def test_same_inputs_ignore_report_identity_and_clock_for_evaluation_hash() -> None:
    first = evaluate_for_promotion(
        request=request(),
        dataset=dataset(),
        baselines=baselines(),
        policy=policy(),
        report_id=UUID("00000000-0000-4000-8000-000000000505"),
        report_artifact_ref=f"sha256:{HASH_D}",
        created_at=NOW,
    )
    second = evaluate_for_promotion(
        request=request(),
        dataset=dataset(),
        baselines=baselines(),
        policy=policy(),
        report_id=UUID("00000000-0000-4000-8000-000000000506"),
        report_artifact_ref="sha256:" + "f" * 64,
        created_at=NOW + timedelta(seconds=1),
    )

    assert isinstance(first, Success)
    assert isinstance(second, Success)
    assert first.value.evaluation_hash == second.value.evaluation_hash


def test_contamination_produces_no_evaluation_report() -> None:
    observations = list(dataset().observations)
    observations[0] = observations[0].model_copy(
        update={
            "feature_available_at": observations[0].prediction_at + timedelta(seconds=1)
        }
    )

    result = evaluate_for_promotion(
        request=request(),
        dataset=dataset(observations=tuple(observations)),
        baselines=baselines(),
        policy=policy(),
        report_id=UUID("00000000-0000-4000-8000-000000000505"),
        report_artifact_ref=f"sha256:{HASH_D}",
        created_at=NOW,
    )

    assert isinstance(result, Failure)


def test_uncalibrated_candidate_returns_rejected_report_with_explicit_check() -> None:
    observations = tuple(
        value.model_copy(update={"direction_probability": Decimal("0.5")})
        for value in dataset().observations
    )
    strict_policy = policy().model_copy(
        update={"maximum_calibration_error": Decimal("0.01")}
    )

    result = evaluate_for_promotion(
        request=request().model_copy(
            update={"evaluation_policy_hash": strict_policy.policy_hash}
        ),
        dataset=dataset(observations=observations),
        baselines=baselines(),
        policy=strict_policy,
        report_id=UUID("00000000-0000-4000-8000-000000000507"),
        report_artifact_ref=f"sha256:{HASH_D}",
        created_at=NOW,
    )

    assert isinstance(result, Success)
    assert result.value.passed is False
    assert result.value.calibration is ConfidenceCalibration.UNCALIBRATED
    failed = {
        check.kind.value for check in result.value.checks if check.status == "failed"
    }
    assert "calibration" in failed


def test_baseline_tie_cannot_be_promoted_as_outperformance() -> None:
    value = dataset()
    tied = baselines()[0].model_copy(
        update={
            "predictions": tuple(item.predicted_return for item in value.observations)
        }
    )

    result = evaluate_for_promotion(
        request=request(),
        dataset=value,
        baselines=(tied, *baselines()[1:]),
        policy=policy(),
        report_id=UUID("00000000-0000-4000-8000-000000000508"),
        report_artifact_ref=f"sha256:{HASH_D}",
        created_at=NOW,
    )

    assert isinstance(result, Success)
    assert result.value.passed is False
    failed = {
        check.kind.value for check in result.value.checks if check.status == "failed"
    }
    assert "baseline_comparison" in failed


def test_cost_and_drawdown_thresholds_have_separate_failed_checks() -> None:
    observations = list(dataset().observations)
    observations[12] = observations[12].model_copy(
        update={
            "predicted_return": Decimal("0.03"),
            "actual_return": Decimal("-0.5"),
            "direction_probability": Decimal("0.8"),
        }
    )
    strict_policy = policy().model_copy(
        update={
            "minimum_net_alpha": Decimal("0.1"),
            "maximum_drawdown": Decimal("0.1"),
        }
    )

    result = evaluate_for_promotion(
        request=request().model_copy(
            update={"evaluation_policy_hash": strict_policy.policy_hash}
        ),
        dataset=dataset(observations=tuple(observations)),
        baselines=baselines(),
        policy=strict_policy,
        report_id=UUID("00000000-0000-4000-8000-000000000509"),
        report_artifact_ref=f"sha256:{HASH_D}",
        created_at=NOW,
    )

    assert isinstance(result, Success)
    failed = {
        check.kind.value for check in result.value.checks if check.status == "failed"
    }
    assert {"cost_sensitivity", "drawdown"} <= failed


def test_policy_hash_changes_with_any_threshold_change() -> None:
    original = policy()
    changed = original.model_copy(update={"fee_bps": Decimal("2")})

    assert original.policy_hash != changed.policy_hash
