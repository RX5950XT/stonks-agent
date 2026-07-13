"""Canonical market snapshot to immutable Qlib tabular artifact conversion."""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise
from typing import Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_contracts.common import (
    ArtifactRef,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)
from stonks_contracts.market_data import Bar, BarSeries, DataQualityStatus
from stonks_contracts.quant_lab import (
    QuantDatasetArtifact,
    QuantDatasetRow,
    QuantFeatureName,
    QuantFeatureSpec,
    QuantLabelSpec,
    QuantUniverseSpec,
)

_ALLOWED_QUALITY = frozenset({DataQualityStatus.AVAILABLE, DataQualityStatus.FALLBACK})


class QuantInstrumentHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: UUID
    series: BarSeries
    historical_universe_known_at: UTCDateTime
    in_historical_universe: bool

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.instrument_id != self.series.instrument_id:
            raise ValueError("quant history instrument binding is invalid")
        return self


class QuantSnapshotConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_snapshot_id: UUID
    source_snapshot_artifact_ref: ArtifactRef
    source_data_hash: Sha256
    as_of: UTCDateTime
    feature_spec: QuantFeatureSpec
    label_spec: QuantLabelSpec
    universe_spec: QuantUniverseSpec
    histories: tuple[QuantInstrumentHistory, ...]


def convert_quant_snapshot(
    request: QuantSnapshotConversionRequest,
) -> Result[QuantDatasetArtifact]:
    """Build deterministic features and proven forward labels without Qlib in core."""
    if not _request_is_valid(request):
        return _failure("Canonical quant snapshot input is invalid")
    try:
        rows = tuple(
            sorted(
                (
                    row
                    for history in request.histories
                    for row in _history_rows(request, history)
                ),
                key=lambda value: (value.event_at, value.instrument_id.hex),
            )
        )
        artifact = QuantDatasetArtifact(
            dataset_snapshot_id=request.dataset_snapshot_id,
            source_snapshot_artifact_ref=request.source_snapshot_artifact_ref,
            source_data_hash=request.source_data_hash,
            as_of=request.as_of,
            feature_spec=request.feature_spec,
            label_spec=request.label_spec,
            universe_spec=request.universe_spec,
            rows=rows,
        )
    except (ArithmeticError, ValueError):
        return _failure("Canonical quant snapshot conversion failed")
    return Success(artifact)


def _request_is_valid(request: QuantSnapshotConversionRequest) -> bool:
    histories = request.histories
    identifiers = tuple(value.instrument_id for value in histories)
    return (
        bool(histories)
        and len(identifiers) == len(set(identifiers))
        and set(identifiers) == set(request.universe_spec.instrument_ids)
        and all(_history_is_valid(request, value) for value in histories)
    )


def _history_is_valid(
    request: QuantSnapshotConversionRequest,
    history: QuantInstrumentHistory,
) -> bool:
    series = history.series
    bars = series.bars
    required = request.feature_spec.lookback_bars + request.label_spec.horizon_bars
    return (
        history.in_historical_universe
        and bool(bars)
        and len(bars) >= required
        and history.instrument_id == series.instrument_id
        and history.historical_universe_known_at <= bars[0].event_time
        and series.as_of == request.as_of
        and series.interval == "1d"
        and series.quality.status in _ALLOWED_QUALITY
        and series.quality.completeness == 1
        and all(
            value.close > 0 and value.event_time <= value.available_at <= request.as_of
            for value in bars
        )
        and not any(
            current.event_time <= previous.event_time
            for previous, current in pairwise(bars)
        )
        and (
            QuantFeatureName.VOLUME_CHANGE_1 not in request.feature_spec.names
            or all(value.volume > 0 for value in bars)
        )
    )


def _history_rows(
    request: QuantSnapshotConversionRequest,
    history: QuantInstrumentHistory,
) -> tuple[QuantDatasetRow, ...]:
    bars = history.series.bars
    first = request.feature_spec.lookback_bars - 1
    stop = len(bars) - request.label_spec.horizon_bars
    return tuple(_row(request, history, bars, index) for index in range(first, stop))


def _row(
    request: QuantSnapshotConversionRequest,
    history: QuantInstrumentHistory,
    bars: tuple[Bar, ...],
    index: int,
) -> QuantDatasetRow:
    event = bars[index]
    outcome = bars[index + request.label_spec.horizon_bars]
    features = tuple(_feature(name, bars, index) for name in request.feature_spec.names)
    identity = stable_payload_hash(
        {
            "snapshot_id": str(request.dataset_snapshot_id),
            "instrument_id": str(history.instrument_id),
            "event_at": event.event_time.isoformat(),
            "feature_spec_hash": request.feature_spec.spec_hash,
            "label_spec_hash": request.label_spec.spec_hash,
        }
    )
    window = bars[index - request.feature_spec.lookback_bars + 1 : index + 1]
    return QuantDatasetRow(
        row_id=uuid5(NAMESPACE_URL, identity),
        instrument_id=history.instrument_id,
        event_at=event.event_time,
        feature_available_at=max(value.available_at for value in window),
        label_outcome_at=outcome.event_time,
        label_available_at=outcome.available_at,
        historical_universe_known_at=history.historical_universe_known_at,
        in_historical_universe=True,
        features=features,
        label=outcome.close / event.close - 1,
    )


def _feature(name: QuantFeatureName, bars: tuple[Bar, ...], index: int) -> Decimal:
    if name is QuantFeatureName.RETURN_1:
        return bars[index].close / bars[index - 1].close - 1
    if name is QuantFeatureName.RETURN_5:
        return bars[index].close / bars[index - 5].close - 1
    if name is QuantFeatureName.VOLATILITY_5:
        returns = tuple(
            bars[position].close / bars[position - 1].close - 1
            for position in range(index - 4, index + 1)
        )
        return _population_deviation(returns)
    if name is QuantFeatureName.VOLUME_CHANGE_1:
        return bars[index].volume / bars[index - 1].volume - 1
    raise ValueError("unsupported quant feature")


def _population_deviation(values: tuple[Decimal, ...]) -> Decimal:
    average = sum(values, Decimal(0)) / Decimal(len(values))
    variance = sum((value - average) ** 2 for value in values) / Decimal(len(values))
    return variance.sqrt()


def _failure(message: str) -> Failure:
    return Failure(StructuredError(code=ErrorCode.INVALID_INPUT, message=message))
