"""Validated read-only latest-available market-data projections."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from stonks_contracts.common import (
    DecimalString,
    NonNegativeDecimal,
    UTCDateTime,
)


class BarInterval(StrEnum):
    """Public bar resolutions this system is proven to serve."""

    MINUTE = "1m"
    TWO_MINUTE = "2m"
    FIVE_MINUTE = "5m"
    FIFTEEN_MINUTE = "15m"
    THIRTY_MINUTE = "30m"
    NINETY_MINUTE = "90m"
    HOUR = "1h"
    DAY = "1d"
    WEEK = "1W"
    MONTH = "1M"
    YEAR = "1Y"

    @property
    def is_intraday(self) -> bool:
        return self in {
            BarInterval.MINUTE,
            BarInterval.TWO_MINUTE,
            BarInterval.FIVE_MINUTE,
            BarInterval.FIFTEEN_MINUTE,
            BarInterval.THIRTY_MINUTE,
            BarInterval.NINETY_MINUTE,
            BarInterval.HOUR,
        }

    @property
    def feed_type(self) -> FeedType:
        return "intraday_historical" if self.is_intraday else "end_of_day_historical"


type FeedType = Literal["end_of_day_historical", "intraday_historical"]


class MarketDataFreshness(StrEnum):
    """Session-aware age of the latest event, separate from feed entitlement."""

    CURRENT = "current"
    MARKET_CLOSED = "market_closed"
    DELAYED = "delayed"
    STALE = "stale"
    UNKNOWN = "unknown"


class MarketDataQuality(StrEnum):
    """Backend-owned presentation quality; Browser code must not promote it."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


MarketSymbol = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Z0-9][A-Z0-9.-]*$",
    ),
]
ProviderName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_.:-]*$",
    ),
]
ProviderWarning = Annotated[
    str,
    StringConstraints(min_length=1, max_length=512),
]
# Provider and canonical response limits per resolution; requesting more can
# return an oversized or empty result, so the boundary is enforced first.
_INTRADAY_MAX_LOOKBACK_DAYS: dict[BarInterval, int] = {
    BarInterval.MINUTE: 7,
    BarInterval.TWO_MINUTE: 21,
    BarInterval.FIVE_MINUTE: 59,
    BarInterval.FIFTEEN_MINUTE: 59,
    BarInterval.THIRTY_MINUTE: 59,
    BarInterval.NINETY_MINUTE: 59,
}
MAX_LOOKBACK_DAYS = 36_525
# ponytail: one bounded response supports the requested all-history daily view;
# raise only with measured provider payload and browser performance evidence.
MAX_BARS = 20_000


class LatestMarketDataQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: MarketSymbol
    lookback_days: int = Field(default=30, ge=1, le=MAX_LOOKBACK_DAYS)
    interval: BarInterval = BarInterval.DAY

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_intraday_window(self) -> Self:
        # yfinance refuses long intraday windows; asking anyway returns an
        # empty body that must never be mistaken for a legitimate empty result.
        maximum = _INTRADAY_MAX_LOOKBACK_DAYS.get(self.interval)
        if maximum is not None and self.lookback_days > maximum:
            raise ValueError(
                f"{self.interval.value} bars accept at most {maximum} lookback days"
            )
        return self


class LatestMarketBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_time: UTCDateTime
    open: DecimalString
    high: DecimalString
    low: DecimalString
    close: DecimalString
    volume: NonNegativeDecimal

    @model_validator(mode="after")
    def validate_ohlc(self) -> Self:
        if self.high < self.low:
            raise ValueError("market-data high must not be below low")
        if not self.low <= self.open <= self.high:
            raise ValueError("market-data open must be within low/high")
        if not self.low <= self.close <= self.high:
            raise ValueError("market-data close must be within low/high")
        return self


class LatestMarketDataObservation(BaseModel):
    """One real provider observation before presentation mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: MarketSymbol
    provider: ProviderName
    feed_type: FeedType
    interval: BarInterval = BarInterval.DAY
    observed_at: UTCDateTime
    bars: tuple[LatestMarketBar, ...] = Field(min_length=1, max_length=MAX_BARS)
    warnings: tuple[ProviderWarning, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.feed_type != self.interval.feed_type:
            raise ValueError("market-data feed type must match its interval")
        times = tuple(bar.event_time for bar in self.bars)
        if times != tuple(sorted(times)) or len(times) != len(set(times)):
            raise ValueError("market-data bars must be unique and ordered")
        if any(value > self.observed_at for value in times):
            raise ValueError("market-data event cannot follow observation")
        return self


class MarketQuoteView(BaseModel):
    """Compact derived quote; never a real-time tick and always says so."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: MarketSymbol
    provider: ProviderName
    feed_type: FeedType
    interval: BarInterval
    is_real_time: Literal[False] = False
    observed_at: UTCDateTime
    served_at: UTCDateTime
    latest_event_time: UTCDateTime
    data_age_seconds: int = Field(ge=0)
    freshness: MarketDataFreshness
    quality: MarketDataQuality
    quality_reasons: tuple[str, ...] = Field(default=(), max_length=16)
    cache_hit: bool = False
    latest: LatestMarketBar
    previous_close: DecimalString | None = None
    change: DecimalString | None = None
    change_percent: DecimalString | None = None
    warnings: tuple[ProviderWarning, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        if self.served_at < self.observed_at:
            raise ValueError("market-data delivery cannot predate its observation")
        if (self.previous_close is None) != (self.change is None):
            raise ValueError("quote change requires a previous close")
        if self.previous_close is not None and self.change_percent is None:
            raise ValueError("quote change requires a percentage")
        return self


class LatestMarketDataView(MarketQuoteView):
    """Full bar-series projection used by charts and provenance panels."""

    bars: tuple[LatestMarketBar, ...] = Field(min_length=1, max_length=MAX_BARS)
