"""Real latest-available market data through the isolated OpenBB sidecar."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from pydantic import ValidationError

from stonks_agent.adapters.market_data.openbb_rest import OpenBBRestAdapter
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
from stonks_agent.ports.service_credentials import ServiceCredentialProvider


class OpenBBLatestMarketDataSource:
    """Map one authenticated OpenBB observation into the GUI-facing port."""

    __slots__ = ("_client", "_credentials")

    def __init__(
        self,
        *,
        client: httpx.Client,
        credentials: ServiceCredentialProvider,
    ) -> None:
        self._client = client
        self._credentials = credentials

    def fetch(
        self,
        query: LatestMarketDataQuery,
        *,
        observed_at: datetime,
    ) -> Result[LatestMarketDataObservation]:
        fixed_time = _normalize_observed_at(observed_at)
        if isinstance(fixed_time, Failure):
            return fixed_time
        request = _request(query, fixed_time.value)
        observation = OpenBBRestAdapter(
            client=self._client,
            credentials=self._credentials,
            clock=lambda: fixed_time.value,
            max_response_bytes=4_194_304,
        ).fetch(request)
        if observation.state is not ProviderDataState.AVAILABLE:
            return _provider_failure(observation.state)
        if observation.metadata is None:
            return _failure(ErrorCode.CONFLICT, "Provider provenance is unavailable")
        try:
            provider_bars = tuple(
                LatestMarketBar(
                    event_time=item.bar.timeline.event_time,
                    open=item.bar.open,
                    high=item.bar.high,
                    low=item.bar.low,
                    close=item.bar.close,
                    volume=item.bar.volume,
                )
                for item in observation.data
            )
            bars = (
                _annual_bars(provider_bars)
                if query.interval is BarInterval.YEAR
                else provider_bars
            )
            return Success(
                LatestMarketDataObservation(
                    symbol=query.symbol,
                    provider=f"openbb:{observation.metadata.provider}",
                    feed_type=query.interval.feed_type,
                    interval=query.interval,
                    observed_at=fixed_time.value,
                    bars=bars,
                    warnings=tuple(
                        warning.message for warning in observation.metadata.warnings
                    ),
                )
            )
        except (TypeError, ValueError, ValidationError):
            return _failure(ErrorCode.CONFLICT, "Provider data is invalid")


def _request(query: LatestMarketDataQuery, observed_at: datetime) -> FetchDataRequest:
    end_date = observed_at.date()
    start_date = end_date - timedelta(days=query.lookback_days - 1)
    return FetchDataRequest(
        market=market_for_symbol(query.symbol),
        capability="prices",
        as_of=observed_at,
        query={
            "symbol": query.symbol,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "interval": _provider_interval(query.interval),
        },
    )


def _provider_interval(interval: BarInterval) -> str:
    """Use verified monthly bars as the source for core annual aggregation."""

    return BarInterval.MONTH.value if interval is BarInterval.YEAR else interval.value


def _annual_bars(
    bars: tuple[LatestMarketBar, ...],
) -> tuple[LatestMarketBar, ...]:
    groups: dict[int, list[LatestMarketBar]] = {}
    for bar in bars:
        groups.setdefault(bar.event_time.year, []).append(bar)
    return tuple(
        LatestMarketBar(
            event_time=group[-1].event_time,
            open=group[0].open,
            high=max(item.high for item in group),
            low=min(item.low for item in group),
            close=group[-1].close,
            volume=sum((item.volume for item in group), Decimal("0")),
        )
        for year in sorted(groups)
        for group in (groups[year],)
    )


def _normalize_observed_at(value: datetime) -> Result[datetime]:
    if value.tzinfo is None or value.utcoffset() is None:
        return _failure(ErrorCode.CONFIGURATION_INVALID, "Provider clock is invalid")
    return Success(value.astimezone(UTC))


def _provider_failure(state: ProviderDataState) -> Failure:
    if state is ProviderDataState.QUOTA_EXHAUSTED:
        return _failure(ErrorCode.RATE_LIMITED, "Market-data quota is exhausted")
    if state is ProviderDataState.NOT_SUPPORTED:
        return _failure(
            ErrorCode.CAPABILITY_DENIED,
            "Market-data symbol is not supported",
        )
    if state is ProviderDataState.LEGITIMATE_EMPTY:
        return _failure(ErrorCode.NOT_FOUND, "Market data was not found")
    return _failure(
        ErrorCode.DATA_UNAVAILABLE,
        "Latest market data is unavailable",
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
