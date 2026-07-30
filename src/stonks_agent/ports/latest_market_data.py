"""Port for real, read-only latest-available market data."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from stonks_agent.domain.errors import Result
from stonks_agent.domain.latest_market_data import (
    LatestMarketDataObservation,
    LatestMarketDataQuery,
)


class LatestMarketDataSource(Protocol):
    def fetch(
        self,
        query: LatestMarketDataQuery,
        *,
        observed_at: datetime,
    ) -> Result[LatestMarketDataObservation]: ...
