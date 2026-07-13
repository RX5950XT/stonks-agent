"""Simple moving-average close baseline."""

from decimal import Decimal

from stonks_agent.strategies.baselines.common import (
    BaselineAlgorithm,
    BaselineManifest,
    BaselineSeries,
    build_forecast,
    require_lookback,
)
from stonks_contracts.signal import ForecastSignal


class MovingAverageBaseline:
    def __init__(self, manifest: BaselineManifest) -> None:
        if manifest.algorithm is not BaselineAlgorithm.MOVING_AVERAGE:
            raise ValueError("moving-average baseline requires matching manifest")
        self._manifest = manifest

    def forecast(self, series: BaselineSeries) -> ForecastSignal:
        bars = require_lookback(series, self._manifest)
        predicted = sum((value.close for value in bars), Decimal(0)) / len(bars)
        return build_forecast(series, self._manifest, predicted)
