"""Probability calibration buckets, Brier score, and deterministic gate."""

from __future__ import annotations

from decimal import Decimal

from stonks_agent.application.evaluation.contracts import (
    CalibrationBucket,
    CalibrationResult,
    EvaluationDataset,
    EvaluationPolicy,
    mean,
    quantize,
)
from stonks_contracts.common import ConfidenceCalibration


def evaluate_calibration(
    dataset: EvaluationDataset,
    policy: EvaluationPolicy,
) -> CalibrationResult:
    actuals = tuple(
        Decimal(1) if value.actual_return > 0 else Decimal(0)
        for value in dataset.observations
    )
    probabilities = tuple(value.direction_probability for value in dataset.observations)
    brier = quantize(
        mean(
            tuple(
                (probability - actual) ** 2
                for probability, actual in zip(probabilities, actuals, strict=True)
            )
        )
    )
    buckets = _buckets(probabilities, actuals, policy.calibration_buckets)
    error = quantize(
        sum(
            (
                Decimal(bucket.count)
                / Decimal(len(probabilities))
                * abs(bucket.mean_probability - bucket.observed_frequency)
                for bucket in buckets
            ),
            Decimal(0),
        )
    )
    calibrated = (
        len(probabilities) >= policy.minimum_observations
        and brier <= policy.maximum_brier_score
        and error <= policy.maximum_calibration_error
    )
    return CalibrationResult(
        status=(
            ConfidenceCalibration.CALIBRATED
            if calibrated
            else ConfidenceCalibration.UNCALIBRATED
        ),
        brier_score=brier,
        expected_calibration_error=error,
        buckets=buckets,
    )


def _buckets(
    probabilities: tuple[Decimal, ...],
    actuals: tuple[Decimal, ...],
    count: int,
) -> tuple[CalibrationBucket, ...]:
    width = Decimal(1) / Decimal(count)
    values: list[CalibrationBucket] = []
    for index in range(count):
        member_indices = tuple(
            position
            for position, probability in enumerate(probabilities)
            if min(int(probability * count), count - 1) == index
        )
        member_probabilities = tuple(
            probabilities[position] for position in member_indices
        )
        member_actuals = tuple(actuals[position] for position in member_indices)
        values.append(
            CalibrationBucket(
                lower_bound=quantize(width * index),
                upper_bound=quantize(width * (index + 1)),
                count=len(member_indices),
                mean_probability=(
                    quantize(mean(member_probabilities))
                    if member_indices
                    else Decimal(0)
                ),
                observed_frequency=(
                    quantize(mean(member_actuals)) if member_indices else Decimal(0)
                ),
            )
        )
    return tuple(values)
