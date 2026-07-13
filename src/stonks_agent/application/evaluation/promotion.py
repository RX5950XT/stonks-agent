"""End-to-end deterministic evaluation report and promotion gate."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.application.evaluation.calibration import evaluate_calibration
from stonks_agent.application.evaluation.contracts import (
    CalibrationResult,
    CandidatePredictionSeries,
    CostScenario,
    EvaluationDataset,
    EvaluationPolicy,
    PerformanceMetrics,
    WalkForwardSplit,
)
from stonks_agent.application.evaluation.costs import evaluate_cost_sensitivity
from stonks_agent.application.evaluation.leakage import audit_dataset
from stonks_agent.application.evaluation.metrics import calculate_metrics
from stonks_agent.application.evaluation.walk_forward import (
    build_purged_walk_forward_splits,
    estimate_probability_of_backtest_overfitting,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evaluation import (
    EvaluationCheck,
    EvaluationCheckKind,
    EvaluationCheckStatus,
    EvaluationMetric,
    EvaluationReport,
    EvaluationRequest,
)
from stonks_contracts.common import ArtifactRef, ConfidenceCalibration, UTCDateTime


def evaluate_for_promotion(
    *,
    request: EvaluationRequest,
    dataset: EvaluationDataset,
    baselines: tuple[CandidatePredictionSeries, ...],
    policy: EvaluationPolicy,
    report_id: UUID,
    report_artifact_ref: ArtifactRef,
    created_at: UTCDateTime,
) -> Result[EvaluationReport]:
    binding_failure = _validate_binding(request, dataset, baselines, policy, created_at)
    if binding_failure is not None:
        return binding_failure
    audit = audit_dataset(dataset)
    if isinstance(audit, Failure):
        return audit
    splits = build_purged_walk_forward_splits(dataset, policy)
    if isinstance(splits, Failure):
        return splits
    out_of_sample, out_of_sample_baselines = _out_of_sample(
        dataset, baselines, splits.value
    )
    if len(out_of_sample.observations) < policy.minimum_observations:
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Walk-forward out-of-sample observations are insufficient",
            )
        )
    target = calculate_metrics(out_of_sample, policy, cost_multiplier=Decimal(1))
    baseline_metrics = tuple(
        (
            value.candidate_id,
            calculate_metrics(
                _with_predictions(out_of_sample, value),
                policy,
                cost_multiplier=Decimal(1),
            ),
        )
        for value in out_of_sample_baselines
    )
    calibration = evaluate_calibration(out_of_sample, policy)
    costs = evaluate_cost_sensitivity(out_of_sample, policy)
    primary = CandidatePredictionSeries(
        candidate_id=f"{request.manifest.strategy_id}/{request.manifest.strategy_version}",
        predictions=tuple(
            value.predicted_return for value in out_of_sample.observations
        ),
    )
    pbo = estimate_probability_of_backtest_overfitting(
        out_of_sample,
        (*out_of_sample_baselines, primary),
        groups=policy.cpcv_groups,
    )
    checks = _checks(target, baseline_metrics, calibration.status, costs, pbo, policy)
    passed = all(value.status is EvaluationCheckStatus.PASSED for value in checks)
    return Success(
        EvaluationReport(
            report_id=report_id,
            strategy_id=request.manifest.strategy_id,
            strategy_version=request.manifest.strategy_version,
            strategy_manifest_hash=request.manifest.manifest_hash,
            dataset_snapshot_id=request.dataset_snapshot_id,
            data_hash=request.data_hash,
            runtime_hash=request.runtime_hash,
            evaluation_policy_hash=policy.policy_hash,
            as_of=request.as_of,
            window_start=request.window_start,
            window_end=request.window_end,
            checks=checks,
            metrics=_metrics(
                target, baseline_metrics, calibration, pbo, len(splits.value)
            ),
            calibration=calibration.status,
            baseline_ids=tuple(value.candidate_id for value in out_of_sample_baselines),
            report_artifact_ref=report_artifact_ref,
            valid_until=created_at + timedelta(days=policy.report_valid_days),
            created_at=created_at,
            passed=passed,
        )
    )


def _validate_binding(
    request: EvaluationRequest,
    dataset: EvaluationDataset,
    baselines: tuple[CandidatePredictionSeries, ...],
    policy: EvaluationPolicy,
    created_at: datetime,
) -> Failure | None:
    valid = (
        dataset.dataset_snapshot_id == request.dataset_snapshot_id
        and dataset.data_hash == request.data_hash
        and dataset.as_of == request.as_of
        and policy.policy_hash == request.evaluation_policy_hash
        and created_at >= request.as_of
        and created_at <= request.deadline_at
        and len(dataset.observations) >= policy.minimum_observations
        and bool(baselines)
        and len({value.candidate_id for value in baselines}) == len(baselines)
        and all(
            len(value.predictions) == len(dataset.observations) for value in baselines
        )
        and all(
            request.window_start <= value.prediction_at <= request.window_end
            for value in dataset.observations
        )
    )
    if valid:
        return None
    return Failure(
        StructuredError(
            code=ErrorCode.INVALID_INPUT,
            message="Evaluation inputs do not match immutable request binding",
        )
    )


def _with_predictions(
    dataset: EvaluationDataset,
    candidate: CandidatePredictionSeries,
) -> EvaluationDataset:
    observations = tuple(
        value.model_copy(update={"predicted_return": predicted})
        for value, predicted in zip(
            dataset.observations, candidate.predictions, strict=True
        )
    )
    return dataset.model_copy(update={"observations": observations})


def _out_of_sample(
    dataset: EvaluationDataset,
    baselines: tuple[CandidatePredictionSeries, ...],
    splits: tuple[WalkForwardSplit, ...],
) -> tuple[EvaluationDataset, tuple[CandidatePredictionSeries, ...]]:
    selected_ids = {
        identifier for split in splits for identifier in split.test_observation_ids
    }
    selected_indices = tuple(
        index
        for index, value in enumerate(dataset.observations)
        if value.observation_id in selected_ids
    )
    observations = tuple(dataset.observations[index] for index in selected_indices)
    candidates = tuple(
        value.model_copy(
            update={
                "predictions": tuple(
                    value.predictions[index] for index in selected_indices
                )
            }
        )
        for value in baselines
    )
    return dataset.model_copy(update={"observations": observations}), candidates


def _checks(
    target: PerformanceMetrics,
    baselines: tuple[tuple[str, PerformanceMetrics], ...],
    calibration: ConfidenceCalibration,
    costs: tuple[CostScenario, ...],
    pbo: Decimal | None,
    policy: EvaluationPolicy,
) -> tuple[EvaluationCheck, ...]:
    baseline_passed = target.net_alpha > max(value.net_alpha for _, value in baselines)
    worst_cost = min(value.mean_net_return for value in costs)
    cost_passed = worst_cost - target.mean_benchmark_return >= policy.minimum_net_alpha
    risk_passed = abs(target.max_drawdown) <= policy.maximum_drawdown
    pbo_passed = pbo is None or pbo <= policy.max_pbo
    results = {
        EvaluationCheckKind.POINT_IN_TIME: True,
        EvaluationCheckKind.LEAKAGE: True,
        EvaluationCheckKind.SURVIVORSHIP: True,
        EvaluationCheckKind.REPRODUCIBILITY: True,
        EvaluationCheckKind.BASELINE_COMPARISON: baseline_passed,
        EvaluationCheckKind.COST_SENSITIVITY: cost_passed,
        EvaluationCheckKind.DRAWDOWN: risk_passed,
        EvaluationCheckKind.CALIBRATION: (
            calibration is ConfidenceCalibration.CALIBRATED
        ),
        EvaluationCheckKind.OVERFITTING: pbo_passed,
    }
    return tuple(
        EvaluationCheck(
            kind=kind,
            status=(
                EvaluationCheckStatus.PASSED if passed else EvaluationCheckStatus.FAILED
            ),
            reason_codes=() if passed else (f"{kind.value}_failed",),
        )
        for kind, passed in results.items()
    )


def _metrics(
    target: PerformanceMetrics,
    baselines: tuple[tuple[str, PerformanceMetrics], ...],
    calibration: CalibrationResult,
    pbo: Decimal | None,
    split_count: int,
) -> tuple[EvaluationMetric, ...]:
    values = list(_performance_metrics(target, "candidate"))
    for identifier, metrics in baselines:
        values.extend(_performance_metrics(metrics, identifier.replace("/", "-")))
    values.extend(
        (
            EvaluationMetric(
                name="brier_score", value=calibration.brier_score, unit="ratio"
            ),
            EvaluationMetric(
                name="calibration_error",
                value=calibration.expected_calibration_error,
                unit="ratio",
            ),
            EvaluationMetric(
                name="walk_forward_splits", value=Decimal(split_count), unit="count"
            ),
            EvaluationMetric(
                name="out_of_sample_observations",
                value=Decimal(target.observation_count),
                unit="count",
            ),
            EvaluationMetric(name="pbo", value=pbo or Decimal(0), unit="ratio"),
        )
    )
    return tuple(values)


def _performance_metrics(
    value: PerformanceMetrics,
    segment: str,
) -> tuple[EvaluationMetric, ...]:
    return (
        EvaluationMetric(
            name="mean_net_return",
            value=value.mean_net_return,
            unit="return",
            segment=segment,
        ),
        EvaluationMetric(
            name="net_alpha", value=value.net_alpha, unit="return", segment=segment
        ),
        EvaluationMetric(
            name="max_drawdown",
            value=value.max_drawdown,
            unit="return",
            segment=segment,
        ),
        EvaluationMetric(
            name="hit_rate", value=value.hit_rate, unit="ratio", segment=segment
        ),
        EvaluationMetric(
            name="turnover", value=value.mean_turnover, unit="ratio", segment=segment
        ),
        EvaluationMetric(
            name="sharpe", value=value.sharpe_ratio, unit="ratio", segment=segment
        ),
    )
