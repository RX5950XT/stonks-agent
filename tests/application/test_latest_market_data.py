from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from stonks_agent.application.latest_market_data import (
    read_latest_market_data,
    read_market_quote,
    refresh_cached_quote,
)
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.latest_market_data import (
    BarInterval,
    LatestMarketBar,
    LatestMarketDataObservation,
    LatestMarketDataQuery,
    MarketDataFreshness,
    MarketDataQuality,
)

NOW = datetime(2026, 7, 24, 20, tzinfo=UTC)


class RecordingSource:
    def __init__(
        self,
        result: Success[LatestMarketDataObservation] | Failure,
    ) -> None:
        self.result = result
        self.calls: list[tuple[LatestMarketDataQuery, datetime]] = []

    def fetch(
        self,
        query: LatestMarketDataQuery,
        *,
        observed_at: datetime,
    ) -> Success[LatestMarketDataObservation] | Failure:
        self.calls.append((query, observed_at))
        return self.result


def test_latest_market_data_uses_one_exact_clock_and_preserves_provenance() -> None:
    source = RecordingSource(Success(observation()))
    query = LatestMarketDataQuery(symbol="AAPL", lookback_days=30)

    result = read_latest_market_data(query, source=source, clock=lambda: NOW)

    assert isinstance(result, Success)
    assert source.calls == [(query, NOW)]
    assert result.value.symbol == "AAPL"
    assert result.value.provider == "openbb:yfinance"
    assert result.value.feed_type == "end_of_day_historical"
    assert result.value.is_real_time is False
    assert result.value.latest.close == Decimal("191.20")
    assert result.value.latest_event_time == datetime(2026, 7, 24, tzinfo=UTC)
    assert result.value.data_age_seconds == 72_000
    assert result.value.warnings == ("delayed",)
    assert result.value.freshness is MarketDataFreshness.UNKNOWN
    assert result.value.quality is MarketDataQuality.DEGRADED
    assert result.value.quality_reasons == ("provider_warning",)
    assert result.value.served_at == NOW
    assert result.value.cache_hit is False


def test_provider_failure_is_returned_without_fixture_fallback() -> None:
    failure = Failure(
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="Latest market data is unavailable",
        )
    )
    source = RecordingSource(failure)

    result = read_latest_market_data(
        LatestMarketDataQuery(symbol="MSFT"),
        source=source,
        clock=lambda: NOW,
    )

    assert result is failure
    assert len(source.calls) == 1


def test_symbol_drift_and_naive_clock_fail_closed() -> None:
    drifted = observation().model_copy(update={"symbol": "MSFT"})
    source = RecordingSource(Success(drifted))

    mismatch = read_latest_market_data(
        LatestMarketDataQuery(symbol="AAPL"),
        source=source,
        clock=lambda: NOW,
    )
    naive = read_latest_market_data(
        LatestMarketDataQuery(symbol="AAPL"),
        source=source,
        clock=lambda: NOW.replace(tzinfo=None),
    )

    assert isinstance(mismatch, Failure)
    assert mismatch.error.code is ErrorCode.CONFLICT
    assert isinstance(naive, Failure)
    assert naive.error.code is ErrorCode.CONFIGURATION_INVALID
    assert len(source.calls) == 1


@pytest.mark.parametrize(
    ("symbol", "lookback"),
    [
        ("../AAPL", 30),
        ("AAPL", 0),
        ("AAPL", 367),
    ],
)
def test_query_is_bounded(symbol: str, lookback: int) -> None:
    with pytest.raises(ValueError):
        LatestMarketDataQuery(symbol=symbol, lookback_days=lookback)


def test_quote_projection_derives_change_without_shipping_the_series() -> None:
    source = RecordingSource(Success(observation()))

    result = read_market_quote(
        LatestMarketDataQuery(symbol="AAPL"),
        source=source,
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    assert result.value.previous_close == Decimal("189.00")
    assert result.value.change == Decimal("2.20")
    assert result.value.change_percent == Decimal("1.16")
    assert not hasattr(result.value, "bars")


def test_cached_quote_recomputes_age_and_marks_cache_provenance() -> None:
    source = RecordingSource(Success(observation().model_copy(update={"warnings": ()})))
    initial = read_market_quote(
        LatestMarketDataQuery(symbol="AAPL"),
        source=source,
        clock=lambda: NOW,
    )
    assert isinstance(initial, Success)

    refreshed = refresh_cached_quote(
        initial.value,
        served_at=datetime(2026, 7, 24, 20, 0, 17, tzinfo=UTC),
    )

    assert refreshed.data_age_seconds == initial.value.data_age_seconds + 17
    assert refreshed.observed_at == NOW
    assert refreshed.served_at == datetime(2026, 7, 24, 20, 0, 17, tzinfo=UTC)
    assert refreshed.cache_hit is True


def test_single_bar_and_zero_previous_close_withhold_change() -> None:
    single = observation().model_copy(
        update={"bars": (bar("2026-07-24T00:00:00Z", "5"),)}
    )
    zeroed = observation().model_copy(
        update={
            "bars": (
                bar("2026-07-23T00:00:00Z", "0"),
                bar("2026-07-24T00:00:00Z", "5"),
            )
        }
    )

    for candidate in (single, zeroed):
        result = read_market_quote(
            LatestMarketDataQuery(symbol="AAPL"),
            source=RecordingSource(Success(candidate)),
            clock=lambda: NOW,
        )
        assert isinstance(result, Success)
        assert result.value.change is None
        assert result.value.change_percent is None


def test_interval_drift_between_request_and_provider_fails_closed() -> None:
    source = RecordingSource(Success(observation()))

    result = read_latest_market_data(
        LatestMarketDataQuery(
            symbol="AAPL", interval=BarInterval.HOUR, lookback_days=5
        ),
        source=source,
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def test_intraday_query_rejects_windows_the_provider_cannot_serve() -> None:
    assert (
        LatestMarketDataQuery(
            symbol="AAPL", interval=BarInterval.MINUTE, lookback_days=7
        ).interval
        is BarInterval.MINUTE
    )
    with pytest.raises(ValueError):
        LatestMarketDataQuery(
            symbol="AAPL", interval=BarInterval.MINUTE, lookback_days=8
        )
    with pytest.raises(ValueError):
        LatestMarketDataQuery(
            symbol="AAPL", interval=BarInterval.FIVE_MINUTE, lookback_days=60
        )


def test_feed_type_must_match_its_interval() -> None:
    with pytest.raises(ValueError):
        LatestMarketDataObservation(
            symbol="AAPL",
            provider="openbb:yfinance",
            feed_type="end_of_day_historical",
            interval=BarInterval.HOUR,
            observed_at=NOW,
            bars=(bar("2026-07-24T00:00:00Z", "1"),),
        )


def observation() -> LatestMarketDataObservation:
    return LatestMarketDataObservation(
        symbol="AAPL",
        provider="openbb:yfinance",
        feed_type="end_of_day_historical",
        observed_at=NOW,
        bars=(
            bar("2026-07-23T00:00:00Z", "189.00"),
            bar("2026-07-24T00:00:00Z", "191.20"),
        ),
        warnings=("delayed",),
    )


def bar(event_time: str, close: str) -> LatestMarketBar:
    value = Decimal(close)
    return LatestMarketBar(
        event_time=event_time,
        open=value,
        high=value + 1,
        low=value - 1 if value > 0 else value,
        close=value,
        volume="1000000",
    )
