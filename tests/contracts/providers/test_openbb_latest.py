from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
from pydantic import SecretStr

from stonks_agent.adapters.market_data.openbb_latest import OpenBBLatestMarketDataSource
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.latest_market_data import LatestMarketDataQuery
from stonks_agent.ports.service_credentials import (
    ServiceBearerCredential,
    ServiceCredentialRequest,
)

NOW = datetime(2026, 7, 24, 20, tzinfo=UTC)
TOKEN = "test-openbb-service-credential-32-bytes"


class Credentials:
    def __init__(self) -> None:
        self.calls: list[ServiceCredentialRequest] = []

    def issue(
        self,
        request: ServiceCredentialRequest,
    ) -> Success[ServiceBearerCredential]:
        self.calls.append(request)
        return Success(ServiceBearerCredential(token=SecretStr(TOKEN)))


def test_openbb_latest_source_fetches_sidecar_route_shape_without_future_as_of() -> (
    None
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "provider-request",
                "provider": "yfinance",
                "results": [
                    {
                        "date": "2026-07-24",
                        "open": 188,
                        "high": 193,
                        "low": 187,
                        "close": 191.2,
                        "volume": 1_000_000,
                    }
                ],
                "warnings": [{"category": "provider", "message": "delayed"}],
            },
            request=request,
        )

    credentials = Credentials()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OpenBBLatestMarketDataSource(
            client=client,
            credentials=credentials,
        ).fetch(
            LatestMarketDataQuery(symbol="AAPL", lookback_days=30),
            observed_at=NOW,
        )

    assert isinstance(result, Success)
    assert result.value.provider == "openbb:yfinance"
    assert result.value.observed_at == NOW
    assert result.value.bars[0].close == Decimal("191.2")
    assert result.value.warnings == ("delayed",)
    assert len(credentials.calls) == 1
    assert len(requests) == 1
    assert requests[0].url == httpx.URL(
        "http://127.0.0.1:6900/api/v1/equity/price/historical"
        "?symbol=AAPL&start_date=2026-06-25&end_date=2026-07-24"
        "&provider=yfinance"
    )


def test_openbb_unavailable_maps_to_public_safe_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("token=must-not-leak", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OpenBBLatestMarketDataSource(
            client=client,
            credentials=Credentials(),
        ).fetch(
            LatestMarketDataQuery(symbol="AAPL"),
            observed_at=NOW,
        )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert result.error.message == "Latest market data is unavailable"
    assert "must-not-leak" not in repr(result)
