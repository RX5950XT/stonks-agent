from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from stonks_agent.adapters.market_data.openbb_rest import OpenBBRestAdapter
from stonks_agent.adapters.market_data.openbb_snapshot import (
    OpenBBSnapshotMaterializationSource,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.ports.service_credentials import (
    ServiceBearerCredential,
    ServiceCredentialRequest,
)

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


class Credentials:
    def issue(
        self,
        request: ServiceCredentialRequest,
    ) -> Success[ServiceBearerCredential]:
        return Success(ServiceBearerCredential(token=SecretStr("x" * 32)))


def request() -> FetchDataRequest:
    return FetchDataRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={
            "symbol": "AAPL",
            "start_date": "2026-07-24",
            "end_date": "2026-07-24",
        },
    )


def source(
    handler: httpx.MockTransport,
) -> tuple[OpenBBSnapshotMaterializationSource, httpx.Client]:
    client = httpx.Client(transport=handler)
    adapter = OpenBBRestAdapter(
        client=client,
        credentials=Credentials(),
        clock=lambda: NOW,
    )
    return OpenBBSnapshotMaterializationSource(adapter), client


def test_actual_response_bytes_are_preserved_and_normalized_to_evidence() -> None:
    raw = json.dumps(
        {
            "id": "request-1",
            "provider": "yfinance",
            "warnings": [],
            "extra": {},
            "results": [
                {
                    "date": "2026-07-24",
                    "open": 100,
                    "high": 105,
                    "low": 99,
                    "close": 104,
                    "volume": 1234,
                }
            ],
        },
        separators=(", ", ": "),
    ).encode()

    def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/json"},
            request=incoming,
        )

    adapter, client = source(httpx.MockTransport(handler))
    try:
        result = adapter.fetch(request(), provider_policy_id="us-prices/1")
    finally:
        client.close()

    assert isinstance(result, Success)
    assert result.value.raw_payload == raw
    assert result.value.provider == "openbb_rest"
    assert result.value.endpoint == "/api/v1/equity/price/historical"
    assert len(result.value.evidence) == 1
    item = result.value.evidence[0]
    assert item.subject == "instrument:aapl"
    assert item.payload["close"] == "104"
    assert item.timeline.available_at == NOW
    assert result.value.observation.data == (item.payload,)


def test_empty_provider_result_fails_closed_instead_of_creating_snapshot() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "request-1",
                "provider": "yfinance",
                "warnings": [],
                "extra": {},
                "results": [],
            },
            request=incoming,
        )

    adapter, client = source(httpx.MockTransport(handler))
    try:
        result = adapter.fetch(request(), provider_policy_id="us-prices/1")
    finally:
        client.close()

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE


def test_policy_mismatch_and_provider_failure_are_typed() -> None:
    calls = 0

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=incoming)

    adapter, client = source(httpx.MockTransport(handler))
    try:
        wrong_policy = adapter.fetch(request(), provider_policy_id="other-policy")
        unavailable = adapter.fetch(request(), provider_policy_id="us-prices/1")
    finally:
        client.close()

    assert isinstance(wrong_policy, Failure)
    assert wrong_policy.error.code is ErrorCode.CAPABILITY_DENIED
    assert isinstance(unavailable, Failure)
    assert unavailable.error.code is ErrorCode.DATA_UNAVAILABLE
    assert calls == 1
