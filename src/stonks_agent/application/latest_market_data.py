"""Read one exact latest-available provider observation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import TypedDict

from pydantic import ValidationError

from stonks_agent.application.market_freshness import MarketFreshnessPolicy
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.latest_market_data import (
    BarInterval,
    FeedType,
    LatestMarketBar,
    LatestMarketDataObservation,
    LatestMarketDataQuery,
    LatestMarketDataView,
    MarketDataFreshness,
    MarketDataQuality,
    MarketQuoteView,
)
from stonks_agent.ports.latest_market_data import LatestMarketDataSource

_PERCENT_QUANTUM = Decimal("0.01")


class _ProjectionFields(TypedDict):
    """Exactly the fields both projections share, kept typed end to end."""

    symbol: str
    provider: str
    feed_type: FeedType
    interval: BarInterval
    observed_at: datetime
    served_at: datetime
    latest_event_time: datetime
    data_age_seconds: int
    freshness: MarketDataFreshness
    quality: MarketDataQuality
    quality_reasons: tuple[str, ...]
    cache_hit: bool
    latest: LatestMarketBar
    previous_close: Decimal | None
    change: Decimal | None
    change_percent: Decimal | None
    warnings: tuple[str, ...]


def read_latest_market_data(
    query: LatestMarketDataQuery,
    *,
    source: LatestMarketDataSource,
    clock: Callable[[], datetime],
    freshness: MarketFreshnessPolicy | None = None,
) -> Result[LatestMarketDataView]:
    observation = _observe(query, source=source, clock=clock)
    if isinstance(observation, Failure):
        return observation
    fetched, observed_at = observation.value
    try:
        return Success(
            LatestMarketDataView(
                **_projection_fields(fetched, observed_at, freshness=freshness),
                bars=fetched.bars,
            )
        )
    except (TypeError, ValueError, ValidationError):
        return _failure(ErrorCode.CONFLICT, "Market-data projection is invalid")


def read_market_quote(
    query: LatestMarketDataQuery,
    *,
    source: LatestMarketDataSource,
    clock: Callable[[], datetime],
    freshness: MarketFreshnessPolicy | None = None,
) -> Result[MarketQuoteView]:
    """Project one bounded quote without shipping the whole bar series."""

    observation = _observe(query, source=source, clock=clock)
    if isinstance(observation, Failure):
        return observation
    fetched, observed_at = observation.value
    try:
        return Success(
            MarketQuoteView(
                **_projection_fields(fetched, observed_at, freshness=freshness)
            )
        )
    except (TypeError, ValueError, ValidationError):
        return _failure(ErrorCode.CONFLICT, "Market-data projection is invalid")


def _observe(
    query: LatestMarketDataQuery,
    *,
    source: LatestMarketDataSource,
    clock: Callable[[], datetime],
) -> Result[tuple[LatestMarketDataObservation, datetime]]:
    observed_at = _read_clock(clock)
    if isinstance(observed_at, Failure):
        return observed_at
    try:
        fetched = source.fetch(query, observed_at=observed_at.value)
    except Exception:
        return _failure(
            ErrorCode.DATA_UNAVAILABLE,
            "Latest market data is unavailable",
        )
    if isinstance(fetched, Failure):
        return fetched
    if fetched.value.symbol != query.symbol:
        return _failure(ErrorCode.CONFLICT, "Market-data symbol does not match")
    if fetched.value.interval != query.interval:
        return _failure(ErrorCode.CONFLICT, "Market-data interval does not match")
    return Success((fetched.value, observed_at.value))


def _projection_fields(
    observation: LatestMarketDataObservation,
    observed_at: datetime,
    *,
    freshness: MarketFreshnessPolicy | None,
) -> _ProjectionFields:
    latest = observation.bars[-1]
    previous_close, change, change_percent = _comparison(
        latest.close,
        observation.bars[-2].close if len(observation.bars) > 1 else None,
    )
    freshness_state = _freshness(
        freshness,
        interval=observation.interval,
        latest_event_time=latest.event_time,
        checked_at=observed_at,
    )
    quality, reasons = _quality(freshness_state, observation.warnings)
    return {
        "symbol": observation.symbol,
        "provider": observation.provider,
        "feed_type": observation.feed_type,
        "interval": observation.interval,
        "observed_at": observation.observed_at,
        "served_at": observed_at,
        "latest_event_time": latest.event_time,
        "data_age_seconds": int((observed_at - latest.event_time).total_seconds()),
        "freshness": freshness_state,
        "quality": quality,
        "quality_reasons": reasons,
        "cache_hit": False,
        "latest": latest,
        "previous_close": previous_close,
        "change": change,
        "change_percent": change_percent,
        "warnings": observation.warnings,
    }


def refresh_cached_quote(
    quote: MarketQuoteView,
    *,
    served_at: datetime,
    freshness: MarketFreshnessPolicy | None = None,
) -> MarketQuoteView:
    """Refresh delivery-time fields without hiding the original observation."""

    normalized = _aware_utc(served_at)
    if normalized < quote.observed_at:
        raise ValueError("cached quote cannot be served before it was observed")
    freshness_state = _freshness(
        freshness,
        interval=quote.interval,
        latest_event_time=quote.latest_event_time,
        checked_at=normalized,
    )
    quality, reasons = _quality(freshness_state, quote.warnings)
    return quote.model_copy(
        update={
            "served_at": normalized,
            "data_age_seconds": int(
                (normalized - quote.latest_event_time).total_seconds()
            ),
            "freshness": freshness_state,
            "quality": quality,
            "quality_reasons": reasons,
            "cache_hit": True,
        }
    )


def _freshness(
    policy: MarketFreshnessPolicy | None,
    *,
    interval: BarInterval,
    latest_event_time: datetime,
    checked_at: datetime,
) -> MarketDataFreshness:
    if policy is None:
        return MarketDataFreshness.UNKNOWN
    try:
        return policy.assess(
            interval=interval,
            latest_event_time=latest_event_time,
            checked_at=checked_at,
        )
    except Exception:
        return MarketDataFreshness.UNKNOWN


def _quality(
    freshness: MarketDataFreshness,
    warnings: tuple[str, ...],
) -> tuple[MarketDataQuality, tuple[str, ...]]:
    if warnings:
        return MarketDataQuality.DEGRADED, ("provider_warning",)
    if freshness is MarketDataFreshness.STALE:
        return MarketDataQuality.DEGRADED, ("freshness_stale",)
    if freshness is MarketDataFreshness.UNKNOWN:
        return MarketDataQuality.UNKNOWN, ("freshness_unknown",)
    return MarketDataQuality.AVAILABLE, ()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market-data delivery time must be timezone-aware")
    return value.astimezone(UTC)


def _comparison(
    close: Decimal,
    previous_close: Decimal | None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Publish a comparison only when every part of it is derivable."""

    if previous_close is None:
        return None, None, None
    change = close - previous_close
    try:
        percent = (change / previous_close * 100).quantize(_PERCENT_QUANTUM)
    except (InvalidOperation, DivisionByZero, ZeroDivisionError):
        # A zero or unusable previous close cannot yield an honest ratio, and a
        # half-populated comparison reads as a real one. Withhold all of it.
        return None, None, None
    return previous_close, change, percent


def _read_clock(clock: Callable[[], datetime]) -> Result[datetime]:
    try:
        value = clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return Success(value.astimezone(UTC))
    except Exception:
        return _failure(
            ErrorCode.CONFIGURATION_INVALID,
            "Market-data clock is invalid",
        )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
