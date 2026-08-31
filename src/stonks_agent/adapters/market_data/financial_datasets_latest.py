"""Map the optional Financial Datasets daily feed to the GUI market port."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from pydantic import ValidationError

from stonks_agent.adapters.market_data.financial_datasets import (
    FinancialDatasetsAdapter,
    FinancialDatasetsPrice,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.data_quality import ProviderDataState
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.latest_market_data import (
    BarInterval,
    LatestMarketBar,
    LatestMarketDataObservation,
    LatestMarketDataQuery,
)
from stonks_agent.domain.market_region import market_for_symbol


class FinancialDatasetsLatestMarketDataSource:
    """Use Financial Datasets only for its validated US daily capability."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: FinancialDatasetsAdapter) -> None:
        self._adapter = adapter

    def fetch(
        self,
        query: LatestMarketDataQuery,
        *,
        observed_at: datetime,
    ) -> Result[LatestMarketDataObservation]:
        fixed_time = _normalize_observed_at(observed_at)
        if isinstance(fixed_time, Failure):
            return fixed_time
        if (
            market_for_symbol(query.symbol) != "US"
            or query.interval is not BarInterval.DAY
        ):
            return _failure(
                ErrorCode.CAPABILITY_DENIED,
                "Financial Datasets only supports US daily market data",
            )
        try:
            observation = self._adapter.fetch(_request(query, fixed_time.value))
            if observation.state is not ProviderDataState.AVAILABLE:
                return _provider_failure(observation.state)
            bars = tuple(_bar(item) for item in observation.data)
            return Success(
                LatestMarketDataObservation(
                    symbol=query.symbol,
                    provider="financial_datasets",
                    feed_type=query.interval.feed_type,
                    interval=query.interval,
                    observed_at=fixed_time.value,
                    bars=bars,
                )
            )
        except (TypeError, ValueError, ValidationError):
            return _failure(ErrorCode.CONFLICT, "Financial data is invalid")


def _request(query: LatestMarketDataQuery, observed_at: datetime) -> FetchDataRequest:
    end_date = observed_at.date()
    start_date = end_date - timedelta(days=query.lookback_days - 1)
    return FetchDataRequest(
        market="US",
        capability="prices",
        as_of=observed_at,
        query={
            "symbol": query.symbol,
            "interval": query.interval.value,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "scenario": "canonical",
        },
    )


def _bar(item: FinancialDatasetsPrice) -> LatestMarketBar:
    return LatestMarketBar(
        event_time=datetime.combine(item.time, time.min, tzinfo=UTC),
        open=item.open,
        high=item.high,
        low=item.low,
        close=item.close,
        volume=Decimal(item.volume),
    )


def _normalize_observed_at(value: datetime) -> Result[datetime]:
    if value.tzinfo is None or value.utcoffset() is None:
        return _failure(ErrorCode.CONFIGURATION_INVALID, "Provider clock is invalid")
    return Success(value.astimezone(UTC))


def _provider_failure(state: ProviderDataState) -> Failure:
    if state is ProviderDataState.QUOTA_EXHAUSTED:
        return _failure(ErrorCode.RATE_LIMITED, "Market-data quota is exhausted")
    if state is ProviderDataState.NOT_SUPPORTED:
        return _failure(ErrorCode.CAPABILITY_DENIED, "Market data is not supported")
    if state is ProviderDataState.LEGITIMATE_EMPTY:
        return _failure(ErrorCode.NOT_FOUND, "Market data was not found")
    return _failure(ErrorCode.DATA_UNAVAILABLE, "Financial data source is unavailable")


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
