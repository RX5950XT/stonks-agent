from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from stonks_agent.adapters.market_data.financial_datasets import (
    FINANCIAL_DATASETS_ORIGIN,
    HISTORICAL_PRICES_ENDPOINT,
    FinancialDatasetsAdapter,
    FinancialDatasetsPrice,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.data_quality import ProviderDataState

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
API_KEY = "fd-test-key-super-secret"


def fetch_request(
    *,
    market: str = "US",
    capability: str = "prices",
    query: dict[str, object] | None = None,
    as_of: datetime = NOW,
) -> FetchDataRequest:
    return FetchDataRequest(
        market=market,
        capability=capability,
        as_of=as_of,
        query=query
        or {
            "ticker": "AAPL",
            "interval": "day",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
        },
    )


def prices_payload(*, ticker: str = "AAPL") -> dict[str, object]:
    return {
        "ticker": ticker,
        "prices": [
            {
                "ticker": ticker,
                "open": 243.85,
                "close": 243.36,
                "high": 244.15,
                "low": 241.91,
                "volume": 40_230_800,
                "time": "2026-01-02",
            }
        ],
    }


def build_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str | None = API_KEY,
    request_budget: int = 10,
    timeout_seconds: float = 1.25,
    max_response_bytes: int = 1_048_576,
) -> tuple[FinancialDatasetsAdapter, httpx.Client]:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    adapter = FinancialDatasetsAdapter(
        client=client,
        api_key=api_key,
        request_budget=request_budget,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        clock=lambda: NOW,
    )
    return adapter, client


def test_valid_response_uses_fixed_endpoint_and_returns_frozen_dto() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        assert request.url.host == "api.financialdatasets.ai"
        assert request.url.path == "/prices"
        assert dict(request.url.params) == {
            "ticker": "AAPL",
            "interval": "day",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
        }
        assert request.headers["X-API-KEY"] == API_KEY
        assert request.extensions["timeout"]["read"] == 1.25
        return httpx.Response(200, json=prices_payload(), request=request)

    adapter, client = build_adapter(handler)
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert FINANCIAL_DATASETS_ORIGIN == "https://api.financialdatasets.ai"
    assert HISTORICAL_PRICES_ENDPOINT == "/prices"
    assert observation.state is ProviderDataState.AVAILABLE
    assert observation.completeness == Decimal("1")
    assert observation.observed_at == NOW
    assert observation.data == (
        FinancialDatasetsPrice(
            ticker="AAPL",
            open=Decimal("243.85"),
            close=Decimal("243.36"),
            high=Decimal("244.15"),
            low=Decimal("241.91"),
            volume=40_230_800,
            time=date(2026, 1, 2),
        ),
    )
    with pytest.raises(ValidationError):
        observation.data[0].open = Decimal("1")  # type: ignore[misc]
    assert API_KEY not in repr(adapter)


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_missing_api_key_is_config_missing_without_network(api_key: str | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network must not be called: {request.url}")

    adapter, client = build_adapter(handler, api_key=api_key)
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.CONFIG_MISSING
    assert observation.reasons == ("api_key_missing",)
    assert observation.data == ()


def test_empty_prices_is_explicit_legitimate_empty() -> None:
    adapter, client = build_adapter(
        lambda request: httpx.Response(
            200,
            json={"ticker": "AAPL", "prices": []},
            request=request,
        )
    )
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.LEGITIMATE_EMPTY
    assert observation.completeness == Decimal("1")
    assert observation.data == ()


@pytest.mark.parametrize(
    ("status_code", "state", "reason"),
    [
        (401, ProviderDataState.CONFIG_MISSING, "provider_auth_failed"),
        (403, ProviderDataState.CONFIG_MISSING, "provider_auth_failed"),
        (402, ProviderDataState.QUOTA_EXHAUSTED, "provider_quota_exhausted"),
        (429, ProviderDataState.QUOTA_EXHAUSTED, "provider_quota_exhausted"),
        (404, ProviderDataState.NOT_SUPPORTED, "ticker_not_supported"),
        (400, ProviderDataState.FETCH_FAILED, "http_status_400"),
        (500, ProviderDataState.FETCH_FAILED, "http_status_500"),
    ],
)
def test_http_errors_map_to_typed_states_without_body_leakage(
    status_code: int,
    state: ProviderDataState,
    reason: str,
) -> None:
    adapter, client = build_adapter(
        lambda request: httpx.Response(
            status_code,
            text=f"provider error containing {API_KEY}",
            request=request,
        )
    )
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is state
    assert observation.reasons == (reason,)
    assert API_KEY not in repr(observation)


def test_timeout_maps_to_fetch_failed_without_exception_leakage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timeout with {API_KEY}", request=request)

    adapter, client = build_adapter(handler)
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("timeout",)
    assert API_KEY not in repr(observation)


def test_transport_error_maps_to_fetch_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    adapter, client = build_adapter(handler)
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("transport_error",)


def test_local_request_budget_is_enforced_before_second_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=prices_payload(), request=request)

    adapter, client = build_adapter(handler, request_budget=1)
    try:
        first = adapter.fetch(fetch_request())
        second = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert first.state is ProviderDataState.AVAILABLE
    assert second.state is ProviderDataState.QUOTA_EXHAUSTED
    assert second.reasons == ("local_rate_budget_exhausted",)
    assert calls == 1
    assert adapter.remaining_requests == 0


def test_response_size_limit_fails_closed() -> None:
    adapter, client = build_adapter(
        lambda request: httpx.Response(200, content=b"x" * 257, request=request),
        max_response_bytes=256,
    )
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("response_too_large",)


def test_declared_response_size_limit_fails_closed() -> None:
    adapter, client = build_adapter(
        lambda request: httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Length": "257"},
            request=request,
        ),
        max_response_bytes=256,
    )
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("response_too_large",)


def test_invalid_content_length_fails_closed() -> None:
    adapter, client = build_adapter(
        lambda request: httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Length": "not-an-integer"},
            request=request,
        )
    )
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("invalid_content_length",)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"request_budget": -1}, "request_budget"),
        ({"request_budget": 1.5}, "request_budget"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": float("inf")}, "timeout_seconds"),
        ({"timeout_seconds": "1"}, "timeout_seconds"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"max_response_bytes": 1.5}, "max_response_bytes"),
    ],
)
def test_invalid_limits_are_rejected_at_configuration_boundary(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client, pytest.raises(ValueError, match=message):
        FinancialDatasetsAdapter(
            client=client,
            api_key=API_KEY,
            **kwargs,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "payload",
    [
        prices_payload() | {"debug": "unexpected"},
        {
            "ticker": "AAPL",
            "prices": [prices_payload()["prices"][0] | {"debug": "unexpected"}],
        },
        {
            "ticker": "AAPL",
            "prices": [prices_payload()["prices"][0] | {"volume": "40230800"}],
        },
        {
            "ticker": "AAPL",
            "prices": [prices_payload()["prices"][0] | {"high": 200}],
        },
    ],
)
def test_external_json_extra_types_and_ohlc_are_strictly_validated(
    payload: dict[str, object],
) -> None:
    adapter, client = build_adapter(
        lambda request: httpx.Response(200, json=payload, request=request)
    )
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("response_schema_invalid",)


def test_malformed_json_fails_closed_without_response_leakage() -> None:
    adapter, client = build_adapter(
        lambda request: httpx.Response(
            200,
            content=f'{{"secret":"{API_KEY}"'.encode(),
            request=request,
        )
    )
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("response_schema_invalid",)
    assert API_KEY not in repr(observation)


@pytest.mark.parametrize(
    "query",
    [
        {
            "ticker": "AAPL",
            "interval": "minute",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
        },
        {
            "ticker": "AAPL",
            "interval": "day",
            "start_date": "2026-01-02",
            "end_date": "2026-01-01",
        },
        {
            "ticker": "AAPL",
            "interval": "day",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "url": "https://attacker.test",
        },
    ],
)
def test_invalid_or_endpoint_injecting_query_never_reaches_network(
    query: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network must not be called: {request.url}")

    adapter, client = build_adapter(handler)
    try:
        observation = adapter.fetch(fetch_request(query=query))
    finally:
        client.close()

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("invalid_request",)


@pytest.mark.parametrize(
    ("market", "capability"),
    [("HK", "prices"), ("US", "news")],
)
def test_unsupported_market_or_capability_never_reaches_network(
    market: str,
    capability: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network must not be called: {request.url}")

    adapter, client = build_adapter(handler)
    try:
        observation = adapter.fetch(
            fetch_request(market=market, capability=capability)
        )
    finally:
        client.close()

    assert observation.state is ProviderDataState.NOT_SUPPORTED
    assert observation.reasons == ("capability_not_supported",)


def test_ticker_mismatch_is_conflict() -> None:
    adapter, client = build_adapter(
        lambda request: httpx.Response(
            200,
            json=prices_payload(ticker="MSFT"),
            request=request,
        )
    )
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.CONFLICT
    assert observation.reasons == ("ticker_mismatch",)


def test_future_bar_is_conflict() -> None:
    payload = prices_payload()
    payload["prices"][0]["time"] = "2026-01-03"  # type: ignore[index]
    adapter, client = build_adapter(
        lambda request: httpx.Response(200, json=payload, request=request)
    )
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.CONFLICT
    assert observation.reasons == ("future_data_returned",)


def test_redirect_is_not_followed_outside_fixed_origin() -> None:
    called_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://attacker.test/steal"},
            request=request,
        )

    adapter, client = build_adapter(handler)
    try:
        observation = adapter.fetch(fetch_request())
    finally:
        client.close()

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("http_status_302",)
    assert len(called_urls) == 1
    assert called_urls[0].startswith(
        f"{FINANCIAL_DATASETS_ORIGIN}{HISTORICAL_PRICES_ENDPOINT}?"
    )
