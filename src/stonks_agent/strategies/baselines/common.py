"""Shared point-in-time contracts and statistics for deterministic baselines."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_contracts.common import Sha256, UTCDateTime, stable_payload_hash
from stonks_contracts.market_data import Bar, DataQuality
from stonks_contracts.signal import ForecastSignal

_QUANTUM = Decimal("0.000000000001")


class BaselineAlgorithm(StrEnum):
    LAST_VALUE = "last_value"
    MOVING_AVERAGE = "moving_average"
    LINEAR = "linear"


class BaselineManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    algorithm: BaselineAlgorithm
    strategy_id: str = Field(pattern=r"^baseline-[a-z][a-z0-9-]{0,119}$")
    strategy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    kind: Literal["deterministic"] = "deterministic"
    lookback_bars: int = Field(ge=1, le=10_000)
    minimum_observations: int = Field(ge=2, le=10_000)
    formula_version: str = Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")
    deterministic: Literal[True] = True
    promotion_state: Literal["draft"] = "draft"

    @model_validator(mode="after")
    def validate_observation_bound(self) -> Self:
        if self.minimum_observations < self.lookback_bars:
            raise ValueError("minimum observations cannot be below lookback")
        return self


class BaselineSeries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: UUID
    dataset_snapshot_id: UUID
    data_hash: Sha256
    as_of: UTCDateTime
    interval: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    horizon_bars: int = Field(ge=1, le=10_000)
    bars: tuple[Bar, ...] = Field(min_length=2, max_length=10_000)
    input_quality: DataQuality

    @model_validator(mode="after")
    def validate_point_in_time_series(self) -> Self:
        times = tuple(value.event_time for value in self.bars)
        if any(current <= previous for previous, current in pairwise(times)):
            raise ValueError("baseline bars must be strictly ordered and unique")
        if any(
            value.event_time > self.as_of or value.available_at > self.as_of
            for value in self.bars
        ):
            raise ValueError("baseline bars must be available by as_of")
        if any(value.close <= 0 for value in self.bars):
            raise ValueError("baseline close prices must be positive")
        return self


def load_baseline_manifests(
    directory: str | Path,
) -> dict[BaselineAlgorithm, BaselineManifest]:
    root = Path(directory)
    manifests: dict[BaselineAlgorithm, BaselineManifest] = {}
    try:
        paths = tuple(sorted(root.glob("*.yaml")))
        for path in paths:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            manifest = BaselineManifest.model_validate(payload)
            if manifest.algorithm in manifests:
                raise ValueError("baseline algorithm manifest is duplicated")
            manifests[manifest.algorithm] = manifest
    except (OSError, yaml.YAMLError, TypeError) as error:
        raise ValueError("baseline manifests could not be loaded") from error
    if set(manifests) != set(BaselineAlgorithm):
        raise ValueError("baseline manifest set is incomplete")
    return manifests


def require_lookback(
    series: BaselineSeries, manifest: BaselineManifest
) -> tuple[Bar, ...]:
    required = max(manifest.lookback_bars, manifest.minimum_observations)
    if len(series.bars) < required:
        raise ValueError("baseline series does not satisfy manifest lookback")
    return series.bars[-manifest.lookback_bars :]


def build_forecast(
    series: BaselineSeries,
    manifest: BaselineManifest,
    predicted_close: Decimal,
) -> ForecastSignal:
    if predicted_close <= 0:
        raise ValueError("baseline predicted close must be positive")
    closes = tuple(value.close for value in series.bars)
    returns = tuple(current / previous - 1 for previous, current in pairwise(closes))
    expected_return = _quantize(predicted_close / closes[-1] - 1)
    volatility = _quantize(_population_deviation(returns))
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "series": series.model_dump(mode="json"),
        "predicted_close": str(_quantize(predicted_close)),
    }
    forecast_id = uuid5(NAMESPACE_URL, stable_payload_hash(payload))
    return ForecastSignal(
        forecast_id=forecast_id,
        instrument_id=series.instrument_id,
        as_of=series.as_of,
        interval=series.interval,
        horizon_bars=series.horizon_bars,
        expected_return=expected_return,
        median_return=_quantize(_median(returns)),
        direction_probability=_direction_probability(expected_return),
        expected_volatility=volatility,
        downside_quantile=_quantize(min(returns)),
        max_drawdown_quantile=_quantize(_max_drawdown(closes)),
        path_count=1,
        dispersion=volatility,
        input_quality=series.input_quality,
        model_id=f"baseline:{manifest.strategy_id}",
        model_revision=manifest.strategy_version,
        tokenizer_id="none",
        tokenizer_revision="none",
        device="cpu",
        seed_policy="deterministic-none",
        inference_code_version=manifest.formula_version,
        dataset_snapshot_id=series.dataset_snapshot_id,
        input_window_start=series.bars[0].event_time,
        input_window_end=series.bars[-1].event_time,
        generated_at=series.as_of,
        latency_ms=0,
        validity_warnings=("research_only_unevaluated",),
    )


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _median(values: tuple[Decimal, ...]) -> Decimal:
    ordered = tuple(sorted(values))
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def _population_deviation(values: tuple[Decimal, ...]) -> Decimal:
    count = Decimal(len(values))
    mean = sum(values, Decimal(0)) / count
    variance = (
        sum(
            ((value - mean) ** 2 for value in values),
            Decimal(0),
        )
        / count
    )
    return variance.sqrt()


def _max_drawdown(closes: tuple[Decimal, ...]) -> Decimal:
    peak = closes[0]
    drawdown = Decimal(0)
    for close in closes[1:]:
        peak = max(peak, close)
        drawdown = min(drawdown, close / peak - 1)
    return drawdown


def _direction_probability(expected_return: Decimal) -> Decimal:
    if expected_return > 0:
        return Decimal(1)
    if expected_return < 0:
        return Decimal(0)
    return Decimal("0.5")
