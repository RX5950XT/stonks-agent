"""Kronos archived-forecast evaluation inputs and promotion reporting."""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.application.evaluation.contracts import (
    CandidatePredictionSeries,
    EvaluationDataset,
    EvaluationObservation,
    EvaluationPolicy,
)
from stonks_agent.application.evaluation.promotion import evaluate_for_promotion
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
)
from stonks_agent.domain.evaluation import EvaluationReport, EvaluationRequest
from stonks_agent.domain.market_region import MARKET_MICS
from stonks_agent.domain.signal import ForecastOutputArtifact
from stonks_agent.domain.strategy import StrategyKind
from stonks_contracts.common import (
    ArtifactRef,
    DecimalString,
    NonNegativeDecimal,
    PositiveDecimal,
    UTCDateTime,
    stable_payload_hash,
)

Market = Literal["US", "HK", "TW"]
AvailabilityCertainty = Literal["proven", "unknown"]
_REQUIRED_BASELINES = (
    "baseline-last-value/1.0.0",
    "baseline-moving-average/1.0.0",
    "baseline-linear/1.0.0",
)


class KronosBaselinePrediction(BaseModel):
    """One baseline prediction aligned to one archived Kronos forecast."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(
        pattern=r"^[a-z][a-z0-9_.-]{0,127}/[0-9]+\.[0-9]+\.[0-9]+$"
    )
    predicted_return: DecimalString


class KronosEvaluationRecord(BaseModel):
    """Immutable prediction, realized label, and baseline comparison row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: UUID
    market: Market
    mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    instrument_id: UUID
    forecast_output: ForecastOutputArtifact
    feature_event_at: UTCDateTime
    feature_available_at: UTCDateTime
    outcome_at: UTCDateTime
    label_available_at: UTCDateTime
    universe_known_at: UTCDateTime
    availability_certainty: AvailabilityCertainty
    in_historical_universe: bool
    label_start_close: PositiveDecimal
    label_end_close: PositiveDecimal
    benchmark_start_close: PositiveDecimal
    benchmark_end_close: PositiveDecimal
    turnover: NonNegativeDecimal
    baselines: tuple[KronosBaselinePrediction, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_archive_binding(self) -> Self:
        forecast = self.forecast_output.forecast
        baseline_ids = tuple(value.candidate_id for value in self.baselines)
        valid = (
            self.mic == MARKET_MICS[self.market]
            and self.instrument_id == forecast.instrument_id
            and self.feature_event_at == forecast.input_window_end
            and self.feature_available_at >= self.feature_event_at
            and len(baseline_ids) == len(set(baseline_ids))
        )
        if not valid:
            raise ValueError("Kronos evaluation record archive binding is invalid")
        return self

    @property
    def actual_return(self) -> Decimal:
        return self.label_end_close / self.label_start_close - 1

    @property
    def benchmark_return(self) -> Decimal:
        return self.benchmark_end_close / self.benchmark_start_close - 1


class KronosEvaluationSnapshot(BaseModel):
    """Content-hashed cross-market evaluation truth from archived outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    as_of: UTCDateTime
    universe_artifact_ref: ArtifactRef
    records: tuple[KronosEvaluationRecord, ...] = Field(
        min_length=2, max_length=1_000_000
    )

    @model_validator(mode="after")
    def validate_identity_and_alignment(self) -> Self:
        identifiers = tuple(value.observation_id for value in self.records)
        prediction_times = tuple(
            value.forecast_output.created_at for value in self.records
        )
        runtimes = {value.forecast_output.runtime_hash for value in self.records}
        model_hashes = {
            value.forecast_output.model_artifact_hash for value in self.records
        }
        tokenizer_hashes = {
            value.forecast_output.tokenizer_artifact_hash for value in self.records
        }
        baseline_ids = tuple(value.candidate_id for value in self.records[0].baselines)
        valid = (
            len(identifiers) == len(set(identifiers))
            and not any(
                current <= previous for previous, current in pairwise(prediction_times)
            )
            and len(runtimes) == 1
            and len(model_hashes) == 1
            and len(tokenizer_hashes) == 1
            and all(
                tuple(item.candidate_id for item in value.baselines) == baseline_ids
                for value in self.records
            )
        )
        if not valid:
            raise ValueError(
                "Kronos evaluation snapshot runtime or baseline alignment is invalid"
            )
        return self

    @property
    def data_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))

    @property
    def runtime_hash(self) -> str:
        return self.records[0].forecast_output.runtime_hash


def build_kronos_evaluation_inputs(
    snapshot: KronosEvaluationSnapshot,
) -> tuple[EvaluationDataset, tuple[CandidatePredictionSeries, ...]]:
    """Materialize generic evaluation inputs without fresh model inference."""
    observations = tuple(_observation(record) for record in snapshot.records)
    dataset = EvaluationDataset(
        dataset_snapshot_id=snapshot.snapshot_id,
        data_hash=snapshot.data_hash,
        as_of=snapshot.as_of,
        universe_artifact_ref=snapshot.universe_artifact_ref,
        observations=observations,
    )
    baseline_ids = tuple(value.candidate_id for value in snapshot.records[0].baselines)
    baselines = tuple(
        CandidatePredictionSeries(
            candidate_id=candidate_id,
            predictions=tuple(
                record.baselines[index].predicted_return for record in snapshot.records
            ),
        )
        for index, candidate_id in enumerate(baseline_ids)
    )
    return dataset, baselines


def evaluate_kronos_snapshot(
    *,
    request: EvaluationRequest,
    snapshot: KronosEvaluationSnapshot,
    policy: EvaluationPolicy,
    report_id: UUID,
    report_artifact_ref: ArtifactRef,
    created_at: UTCDateTime,
) -> Result[EvaluationReport]:
    """Evaluate exact archived outputs with the shared immutable gate."""
    if not _request_matches_snapshot(request, snapshot, policy):
        return Failure(
            StructuredError(
                code=ErrorCode.CONFLICT,
                message="Kronos evaluation request binding mismatch",
            )
        )
    dataset, baselines = build_kronos_evaluation_inputs(snapshot)
    return evaluate_for_promotion(
        request=request,
        dataset=dataset,
        baselines=baselines,
        policy=policy,
        report_id=report_id,
        report_artifact_ref=report_artifact_ref,
        created_at=created_at,
    )


def _observation(record: KronosEvaluationRecord) -> EvaluationObservation:
    forecast = record.forecast_output.forecast
    return EvaluationObservation(
        observation_id=record.observation_id,
        instrument_id=record.instrument_id,
        event_at=record.feature_event_at,
        feature_available_at=record.feature_available_at,
        prediction_at=record.forecast_output.created_at,
        outcome_at=record.outcome_at,
        label_available_at=record.label_available_at,
        universe_known_at=record.universe_known_at,
        availability_certainty=record.availability_certainty,
        in_historical_universe=record.in_historical_universe,
        predicted_return=forecast.expected_return,
        actual_return=record.actual_return,
        benchmark_return=record.benchmark_return,
        direction_probability=forecast.direction_probability,
        turnover=record.turnover,
    )


def _request_matches_snapshot(
    request: EvaluationRequest,
    snapshot: KronosEvaluationSnapshot,
    policy: EvaluationPolicy,
) -> bool:
    return (
        _snapshot_structure_valid(snapshot)
        and request.manifest.kind is StrategyKind.FORECAST_MAPPER
        and request.dataset_snapshot_id == snapshot.snapshot_id
        and request.data_hash == snapshot.data_hash
        and request.snapshot_artifact_ref == f"sha256:{snapshot.data_hash}"
        and request.as_of == snapshot.as_of
        and request.runtime_hash == snapshot.runtime_hash
        and request.evaluation_policy_hash == policy.policy_hash
    )


def _snapshot_structure_valid(snapshot: KronosEvaluationSnapshot) -> bool:
    records = snapshot.records
    if not records:
        return False
    prediction_times = tuple(value.forecast_output.created_at for value in records)
    baseline_ids = tuple(value.candidate_id for value in records[0].baselines)
    return (
        baseline_ids == _REQUIRED_BASELINES
        and {value.market for value in records} == set(MARKET_MICS)
        and len({value.observation_id for value in records}) == len(records)
        and not any(
            current <= previous for previous, current in pairwise(prediction_times)
        )
        and len({value.forecast_output.runtime_hash for value in records}) == 1
        and len({value.forecast_output.model_artifact_hash for value in records}) == 1
        and len({value.forecast_output.tokenizer_artifact_hash for value in records})
        == 1
        and all(
            tuple(item.candidate_id for item in value.baselines) == baseline_ids
            for value in records
        )
    )
