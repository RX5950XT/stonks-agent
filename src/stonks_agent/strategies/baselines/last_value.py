"""Last observed close baseline."""

from stonks_agent.strategies.baselines.common import (
    BaselineAlgorithm,
    BaselineManifest,
    BaselineSeries,
    build_forecast,
    require_lookback,
)
from stonks_contracts.signal import ForecastSignal


class LastValueBaseline:
    def __init__(self, manifest: BaselineManifest) -> None:
        if manifest.algorithm is not BaselineAlgorithm.LAST_VALUE:
            raise ValueError("last-value baseline requires matching manifest")
        self._manifest = manifest

    def forecast(self, series: BaselineSeries) -> ForecastSignal:
        require_lookback(series, self._manifest)
        return build_forecast(series, self._manifest, series.bars[-1].close)
