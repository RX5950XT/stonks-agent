"""Artifact-only contracts for the isolated Qlib quant research worker."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .common import (
    ArtifactRef,
    ContractModel,
    DecimalString,
    NonEmptyString,
    NonNegativeDecimal,
    Sha256,
    SignedUnitDecimal,
    UTCDateTime,
    stable_payload_hash,
)


class QuantFeatureName(StrEnum):
    RETURN_1 = "return_1"
    RETURN_5 = "return_5"
    VOLATILITY_5 = "volatility_5"
    VOLUME_CHANGE_1 = "volume_change_1"


class QuantFeatureSpec(ContractModel):
    names: tuple[QuantFeatureName, ...] = Field(min_length=1, max_length=32)
    lookback_bars: int = Field(ge=2, le=512)

    @model_validator(mode="after")
    def validate_names(self) -> Self:
        if len(self.names) != len(set(self.names)):
            raise ValueError("quant feature names must be unique")
        if QuantFeatureName.RETURN_5 in self.names and self.lookback_bars < 6:
            raise ValueError("return_5 requires at least six lookback bars")
        if QuantFeatureName.VOLATILITY_5 in self.names and self.lookback_bars < 6:
            raise ValueError("volatility_5 requires at least six lookback bars")
        return self

    @property
    def spec_hash(self) -> str:
        return self.payload_hash()


class QuantLabelSpec(ContractModel):
    name: Literal["forward_return"]
    horizon_bars: int = Field(ge=1, le=64)

    @property
    def spec_hash(self) -> str:
        return self.payload_hash()


class QuantUniverseSpec(ContractModel):
    instrument_ids: tuple[UUID, ...] = Field(min_length=1, max_length=10_000)
    historical_membership_artifact_ref: ArtifactRef

    @model_validator(mode="after")
    def validate_instruments(self) -> Self:
        if len(self.instrument_ids) != len(set(self.instrument_ids)):
            raise ValueError("quant universe instruments must be unique")
        return self

    @property
    def spec_hash(self) -> str:
        return self.payload_hash()


class QuantCostModelSpec(ContractModel):
    fee_bps: NonNegativeDecimal
    slippage_bps: NonNegativeDecimal

    @property
    def spec_hash(self) -> str:
        return self.payload_hash()


class QuantSplitSpec(ContractModel):
    train_start: UTCDateTime
    train_end: UTCDateTime
    valid_start: UTCDateTime
    valid_end: UTCDateTime
    test_start: UTCDateTime
    test_end: UTCDateTime
    purge_observations: int = Field(ge=0, le=100_000)
    embargo_observations: int = Field(ge=0, le=100_000)

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if not (
            self.train_start
            <= self.train_end
            < self.valid_start
            <= self.valid_end
            < self.test_start
            <= self.test_end
        ):
            raise ValueError("quant split timeline must be ordered and disjoint")
        return self

    @property
    def spec_hash(self) -> str:
        return self.payload_hash()


class QuantModelSpec(ContractModel):
    algorithm: Literal["qlib_linear_ols"]
    fit_intercept: Literal[False] = False
    deterministic: Literal[True] = True

    @property
    def spec_hash(self) -> str:
        return self.payload_hash()


class QuantRuntimeIdentity(ContractModel):
    worker_version: NonEmptyString = Field(max_length=128)
    qlib_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    qlib_source_hash: Sha256
    qlib_version: NonEmptyString = Field(max_length=128)
    runtime_hash: Sha256
    python_version: NonEmptyString = Field(max_length=64)
    numpy_version: NonEmptyString = Field(max_length=64)
    pandas_version: NonEmptyString = Field(max_length=64)
    sklearn_version: NonEmptyString = Field(max_length=64)


class QuantDatasetRow(ContractModel):
    row_id: UUID
    instrument_id: UUID
    event_at: UTCDateTime
    feature_available_at: UTCDateTime
    label_outcome_at: UTCDateTime
    label_available_at: UTCDateTime
    historical_universe_known_at: UTCDateTime
    in_historical_universe: Literal[True]
    features: tuple[DecimalString, ...] = Field(min_length=1, max_length=32)
    label: DecimalString


class QuantDatasetArtifact(ContractModel):
    dataset_snapshot_id: UUID
    source_snapshot_artifact_ref: ArtifactRef
    source_data_hash: Sha256
    as_of: UTCDateTime
    feature_spec: QuantFeatureSpec
    label_spec: QuantLabelSpec
    universe_spec: QuantUniverseSpec
    rows: tuple[QuantDatasetRow, ...] = Field(min_length=2, max_length=1_000_000)

    @model_validator(mode="after")
    def validate_rows(self) -> Self:
        identities = tuple(value.row_id for value in self.rows)
        ordering = tuple((value.event_at, value.instrument_id.hex) for value in self.rows)
        feature_count = len(self.feature_spec.names)
        valid = (
            len(identities) == len(set(identities))
            and not any(current <= previous for previous, current in pairwise(ordering))
            and all(value.instrument_id in self.universe_spec.instrument_ids for value in self.rows)
            and all(len(value.features) == feature_count for value in self.rows)
        )
        if not valid:
            raise ValueError("quant dataset feature, identity, or ordering is invalid")
        if any(not _row_is_point_in_time(value, self.as_of) for value in self.rows):
            raise ValueError("quant dataset failed point-in-time validation")
        return self


class QuantResearchJob(ContractModel):
    request_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString = Field(max_length=128, repr=False)
    dataset_artifact_ref: ArtifactRef
    dataset: QuantDatasetArtifact
    feature_spec: QuantFeatureSpec
    label_spec: QuantLabelSpec
    universe_spec: QuantUniverseSpec
    cost_model: QuantCostModelSpec
    split_policy: QuantSplitSpec
    model_spec: QuantModelSpec
    runtime: QuantRuntimeIdentity
    requested_at: UTCDateTime
    deadline: UTCDateTime

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        valid = (
            self.dataset_artifact_ref == f"sha256:{self.dataset.payload_hash()}"
            and self.feature_spec == self.dataset.feature_spec
            and self.label_spec == self.dataset.label_spec
            and self.universe_spec == self.dataset.universe_spec
            and self.requested_at >= self.dataset.as_of
            and self.deadline > self.requested_at
        )
        if not valid:
            raise ValueError("quant research job artifact or spec binding is invalid")
        return self


class QuantPrediction(ContractModel):
    row_id: UUID
    instrument_id: UUID
    event_at: UTCDateTime
    predicted_return: DecimalString
    actual_return: DecimalString


class QuantBacktestPosition(ContractModel):
    row_id: UUID
    instrument_id: UUID
    event_at: UTCDateTime
    research_exposure: SignedUnitDecimal
    research_only: Literal[True] = True


class QuantMetric(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    value: DecimalString
    unit: str = Field(pattern=r"^[a-z][a-z0-9_.%/-]{0,63}$")


class QuantResearchResult(ContractModel):
    request_id: UUID
    dataset_snapshot_id: UUID
    source_data_hash: Sha256
    dataset_artifact_hash: Sha256
    feature_spec_hash: Sha256
    label_spec_hash: Sha256
    universe_spec_hash: Sha256
    cost_model_hash: Sha256
    split_policy_hash: Sha256
    model_spec_hash: Sha256
    runtime: QuantRuntimeIdentity
    predictions: tuple[QuantPrediction, ...] = Field(min_length=1, max_length=1_000_000)
    positions: tuple[QuantBacktestPosition, ...] = Field(min_length=1, max_length=1_000_000)
    metrics: tuple[QuantMetric, ...] = Field(min_length=1, max_length=256)
    model_parameters: tuple[DecimalString, ...] = Field(min_length=1, max_length=128)
    prediction_artifact_hash: Sha256
    position_artifact_hash: Sha256
    metrics_artifact_hash: Sha256
    model_artifact_hash: Sha256
    deterministic: Literal[True]
    generated_at: UTCDateTime
    warnings: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def validate_artifact_hashes_and_alignment(self) -> Self:
        prediction_keys = tuple(_prediction_key(value) for value in self.predictions)
        position_keys = tuple(_position_key(value) for value in self.positions)
        aligned = prediction_keys == position_keys and len(prediction_keys) == len(
            set(prediction_keys)
        )
        hashes = (
            self.prediction_artifact_hash == _models_hash(self.predictions)
            and self.position_artifact_hash == _models_hash(self.positions)
            and self.metrics_artifact_hash == _models_hash(self.metrics)
            and self.model_artifact_hash
            == stable_payload_hash([str(value) for value in self.model_parameters])
        )
        if not aligned:
            raise ValueError("quant result prediction and position alignment is invalid")
        if not hashes:
            raise ValueError("quant result artifact hash is invalid")
        return self


class QuantWorkerResponse(ContractModel):
    request_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString = Field(max_length=128, repr=False)
    result_artifact_hash: Sha256
    result: QuantResearchResult

    @model_validator(mode="after")
    def validate_result_hash(self) -> Self:
        if self.result_artifact_hash != self.result.payload_hash():
            raise ValueError("quant result artifact hash is invalid")
        return self


def _row_is_point_in_time(value: QuantDatasetRow, as_of: datetime) -> bool:
    return bool(
        value.event_at <= value.feature_available_at <= as_of
        and value.event_at < value.label_outcome_at <= value.label_available_at <= as_of
        and value.historical_universe_known_at <= value.event_at
    )


def _prediction_key(value: QuantPrediction) -> tuple[UUID, UUID, object]:
    return value.row_id, value.instrument_id, value.event_at


def _position_key(value: QuantBacktestPosition) -> tuple[UUID, UUID, object]:
    return value.row_id, value.instrument_id, value.event_at


def _models_hash(values: tuple[ContractModel, ...]) -> str:
    return stable_payload_hash([value.model_dump(mode="json") for value in values])
