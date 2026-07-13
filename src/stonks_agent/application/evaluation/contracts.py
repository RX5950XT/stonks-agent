"""Closed application contracts for point-in-time strategy evaluation."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks_contracts.common import (
    ArtifactRef,
    ConfidenceCalibration,
    DecimalString,
    NonNegativeDecimal,
    Sha256,
    UnitDecimal,
    UTCDateTime,
    stable_payload_hash,
)

QUANTUM = Decimal("0.000000000001")


class EvaluationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: UUID
    instrument_id: UUID
    event_at: UTCDateTime
    feature_available_at: UTCDateTime
    prediction_at: UTCDateTime
    outcome_at: UTCDateTime
    label_available_at: UTCDateTime
    universe_known_at: UTCDateTime
    availability_certainty: Literal["proven", "unknown"]
    in_historical_universe: bool
    predicted_return: DecimalString
    actual_return: DecimalString
    benchmark_return: DecimalString
    direction_probability: UnitDecimal
    turnover: NonNegativeDecimal

    @field_validator("actual_return", "benchmark_return")
    @classmethod
    def validate_realized_return(cls, value: Decimal) -> Decimal:
        if value < -1:
            raise ValueError("realized returns cannot be below total loss")
        return value


class EvaluationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_snapshot_id: UUID
    data_hash: Sha256
    as_of: UTCDateTime
    universe_artifact_ref: ArtifactRef
    observations: tuple[EvaluationObservation, ...] = Field(
        min_length=2,
        max_length=1_000_000,
    )

    @model_validator(mode="after")
    def validate_identity_and_order(self) -> Self:
        identifiers = tuple(value.observation_id for value in self.observations)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("evaluation observations must be unique")
        times = tuple(value.prediction_at for value in self.observations)
        if any(current <= previous for previous, current in pairwise(times)):
            raise ValueError("evaluation observations must be strictly ordered")
        return self


class CandidatePredictionSeries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(
        pattern=r"^[a-z][a-z0-9_.-]{0,127}/[0-9]+\.[0-9]+\.[0-9]+$"
    )
    predictions: tuple[DecimalString, ...] = Field(min_length=2, max_length=1_000_000)


class EvaluationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_observations: int = Field(ge=2, le=1_000_000)
    train_size: int = Field(ge=2, le=1_000_000)
    test_size: int = Field(ge=1, le=1_000_000)
    step_size: int = Field(ge=1, le=1_000_000)
    purge_observations: int = Field(ge=0, le=100_000)
    embargo_observations: int = Field(ge=0, le=100_000)
    minimum_splits: int = Field(default=1, ge=1, le=10_000)
    cpcv_groups: int = Field(ge=4, le=8)
    max_pbo: UnitDecimal
    fee_bps: NonNegativeDecimal
    slippage_bps: NonNegativeDecimal
    cost_multipliers: tuple[DecimalString, ...] = Field(min_length=1, max_length=16)
    minimum_net_alpha: DecimalString
    maximum_drawdown: UnitDecimal
    maximum_brier_score: UnitDecimal
    maximum_calibration_error: UnitDecimal
    calibration_buckets: int = Field(ge=2, le=20)
    report_valid_days: int = Field(ge=1, le=3650)

    @property
    def policy_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))

    @field_validator("cost_multipliers")
    @classmethod
    def validate_cost_multipliers(
        cls, values: tuple[Decimal, ...]
    ) -> tuple[Decimal, ...]:
        if any(value <= 0 for value in values):
            raise ValueError("cost multipliers must be positive")
        if tuple(sorted(set(values))) != values or Decimal(1) not in values:
            raise ValueError("cost multipliers must be sorted, unique, and include one")
        return values


class EvaluationAuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    point_in_time_passed: Literal[True] = True
    leakage_passed: Literal[True] = True
    survivorship_passed: Literal[True] = True


class WalkForwardSplit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    split_id: int = Field(ge=1)
    train_observation_ids: tuple[UUID, ...] = Field(min_length=2)
    test_observation_ids: tuple[UUID, ...] = Field(min_length=1)
    purge_observations: int = Field(ge=0)
    embargo_observations: int = Field(ge=0)


class CostScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    multiplier: DecimalString
    mean_net_return: DecimalString
    total_cost: NonNegativeDecimal


class PerformanceMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_count: int = Field(ge=1)
    mean_gross_return: DecimalString
    mean_net_return: DecimalString
    mean_benchmark_return: DecimalString
    net_alpha: DecimalString
    max_drawdown: DecimalString
    hit_rate: UnitDecimal
    mean_turnover: NonNegativeDecimal
    sharpe_ratio: DecimalString


class CalibrationBucket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lower_bound: UnitDecimal
    upper_bound: UnitDecimal
    count: int = Field(ge=0)
    mean_probability: UnitDecimal
    observed_frequency: UnitDecimal


class CalibrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ConfidenceCalibration
    brier_score: UnitDecimal
    expected_calibration_error: UnitDecimal
    buckets: tuple[CalibrationBucket, ...] = Field(min_length=2, max_length=20)


def load_evaluation_policy(path: str | Path) -> EvaluationPolicy:
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return EvaluationPolicy.model_validate(payload)
    except (OSError, yaml.YAMLError, TypeError) as error:
        raise ValueError("evaluation policy could not be loaded") from error


def quantize(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise ValueError("cannot calculate mean of empty values")
    return sum(values, Decimal(0)) / Decimal(len(values))


def position(predicted_return: Decimal) -> Decimal:
    if predicted_return > 0:
        return Decimal(1)
    if predicted_return < 0:
        return Decimal(-1)
    return Decimal(0)
