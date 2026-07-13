"""Ordinary least-squares index trend baseline."""

from decimal import Decimal

from stonks_agent.strategies.baselines.common import (
    BaselineAlgorithm,
    BaselineManifest,
    BaselineSeries,
    build_forecast,
    require_lookback,
)
from stonks_contracts.signal import ForecastSignal


class LinearBaseline:
    def __init__(self, manifest: BaselineManifest) -> None:
        if manifest.algorithm is not BaselineAlgorithm.LINEAR:
            raise ValueError("linear baseline requires matching manifest")
        self._manifest = manifest

    def forecast(self, series: BaselineSeries) -> ForecastSignal:
        bars = require_lookback(series, self._manifest)
        count = Decimal(len(bars))
        mean_x = (count - 1) / 2
        mean_y = sum((value.close for value in bars), Decimal(0)) / count
        denominator = sum(
            ((Decimal(index) - mean_x) ** 2 for index in range(len(bars))),
            Decimal(0),
        )
        if denominator == 0:
            raise ValueError("linear baseline requires at least two observations")
        numerator = sum(
            (
                (Decimal(index) - mean_x) * (value.close - mean_y)
                for index, value in enumerate(bars)
            ),
            Decimal(0),
        )
        slope = numerator / denominator
        target_x = Decimal(len(bars) - 1 + series.horizon_bars)
        predicted = mean_y + slope * (target_x - mean_x)
        return build_forecast(series, self._manifest, predicted)
