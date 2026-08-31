from datetime import UTC, datetime
from decimal import Decimal

import httpx
from fixtures.secret_provider import ScriptedSecretProvider

from stonks_agent.adapters.market_data.financial_datasets import (
    FinancialDatasetsAdapter,
)
from stonks_agent.adapters.market_data.financial_datasets_latest import (
    FinancialDatasetsLatestMarketDataSource,
)
from stonks_agent.adapters.market_data.latest_fallback import (
    FailoverLatestMarketDataSource,
)
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.latest_market_data import (
    BarInterval,
    LatestMarketBar,
    LatestMarketDataObservation,
    LatestMarketDataQuery,
)
from stonks_agent.domain.secrets import SecretRef

NOW = datetime(2026, 7, 24, 20, tzinfo=UTC)
QUERY = LatestMarketDataQuery(symbol="AAPL", lookback_days=30, interval=BarInterval.DAY)


def bar(close: str = "191.20") -> LatestMarketBar:
    return LatestMarketBar(
        event_time="2026-07-24T00:00:00Z",
        open="188",
        high="193",
        low="187",
        close=close,
        volume="1000000",
    )


def observation(provider: str = "backup") -> LatestMarketDataObservation:
    return LatestMarketDataObservation(
        symbol="AAPL",
        provider=provider,
        feed_type="end_of_day_historical",
        interval=BarInterval.DAY,
        observed_at=NOW,
        bars=(bar(),),
    )


class StaticSource:
    def __init__(self, result: Success | Failure) -> None:
        self.result = result
        self.calls = 0

    def fetch(self, query: LatestMarketDataQuery, *, observed_at: datetime):
        del query, observed_at
        self.calls += 1
        return self.result


def test_failover_uses_the_second_source_only_after_primary_failure() -> None:
    primary = StaticSource(
        Failure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="primary unavailable",
            )
        )
    )
    secondary = StaticSource(Success(observation()))

    result = FailoverLatestMarketDataSource(
        (("openbb:yfinance", primary), ("financial_datasets", secondary))
    ).fetch(QUERY, observed_at=NOW)

    assert isinstance(result, Success)
    assert result.value.provider == "backup"
    assert result.value.warnings == ("fallback_source_used",)
    assert primary.calls == 1
    assert secondary.calls == 1


def test_failover_does_not_spend_the_fallback_when_primary_succeeds() -> None:
    primary = StaticSource(Success(observation("primary")))
    secondary = StaticSource(Success(observation("secondary")))

    result = FailoverLatestMarketDataSource(
        (("primary", primary), ("secondary", secondary))
    ).fetch(QUERY, observed_at=NOW)

    assert isinstance(result, Success)
    assert result.value.provider == "primary"
    assert result.value.warnings == ()
    assert secondary.calls == 0


def test_failover_returns_attempted_source_codes_when_every_source_fails() -> None:
    primary = StaticSource(
        Failure(StructuredError(code=ErrorCode.DATA_UNAVAILABLE, message="down"))
    )
    secondary = StaticSource(
        Failure(StructuredError(code=ErrorCode.RATE_LIMITED, message="limited"))
    )

    result = FailoverLatestMarketDataSource(
        (("primary", primary), ("secondary", secondary))
    ).fetch(QUERY, observed_at=NOW)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert result.error.details["attempted_sources"] == (
        ("primary", "data_unavailable"),
        ("secondary", "rate_limited"),
    )


def test_financial_datasets_source_maps_daily_price_and_volume() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.financialdatasets.ai"
        return httpx.Response(
            200,
            json={
                "ticker": "AAPL",
                "prices": [
                    {
                        "ticker": "AAPL",
                        "open": 188,
                        "high": 193,
                        "low": 187,
                        "close": 191.2,
                        "volume": 1_000_000,
                        "time": "2026-07-24",
                    }
                ],
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = FinancialDatasetsLatestMarketDataSource(
            FinancialDatasetsAdapter(
                client=client,
                secret_provider=ScriptedSecretProvider(("secret", "test-v1")),
                secret_ref=SecretRef(name="financial_datasets_api_key"),
                clock=lambda: NOW,
            )
        )
        result = source.fetch(QUERY, observed_at=NOW)

    assert isinstance(result, Success)
    assert result.value.provider == "financial_datasets"
    assert result.value.bars[0].close == Decimal("191.2")
    assert result.value.bars[0].volume == Decimal("1000000")


def test_financial_datasets_source_rejects_intraday_without_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url)

    query = QUERY.model_copy(update={"interval": BarInterval.HOUR})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = FinancialDatasetsLatestMarketDataSource(
            FinancialDatasetsAdapter(
                client=client,
                secret_provider=ScriptedSecretProvider(("secret", "test-v1")),
                secret_ref=SecretRef(name="financial_datasets_api_key"),
                clock=lambda: NOW,
            )
        )
        result = source.fetch(query, observed_at=NOW)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED
