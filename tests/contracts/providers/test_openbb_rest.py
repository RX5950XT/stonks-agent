from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from stonks_agent.adapters.market_data.openbb_rest import OpenBBRestAdapter
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.auth import AccessTarget, Permission, ResourceKind
from stonks_agent.domain.data_quality import ProviderDataState
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.ports.service_credentials import (
    ServiceBearerCredential,
    ServiceCredentialRequest,
    ServiceReceiver,
)
from stonks_service_auth import canonical_request_hash

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
ENDPOINT = "/api/v1/equity/price/historical"
TOKEN = "test-openbb-service-credential-32-bytes"


class RecordingCredentialProvider:
    def __init__(
        self,
        result: Result[ServiceBearerCredential] | None = None,
    ) -> None:
        self.result = result or Success(ServiceBearerCredential(token=SecretStr(TOKEN)))
        self.calls: list[ServiceCredentialRequest] = []

    def issue(
        self,
        request: ServiceCredentialRequest,
    ) -> Result[ServiceBearerCredential]:
        self.calls.append(request)
        return self.result


def _adapter(
    *,
    client: httpx.Client,
    credentials: RecordingCredentialProvider | None = None,
    **kwargs: object,
) -> OpenBBRestAdapter:
    return OpenBBRestAdapter(
        client=client,
        credentials=credentials or RecordingCredentialProvider(),
        **kwargs,
    )


def request(**query: object) -> FetchDataRequest:
    return FetchDataRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL", **query},
    )


def response_payload(
    *,
    provider: str = "yfinance",
    results: object = None,
) -> dict[str, object]:
    records = (
        [
            {
                "date": "2026-01-02",
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 104.0,
                "volume": 1234,
                "future_provider_field": {"kept": True},
            }
        ]
        if results is None
        else results
    )
    return {
        "id": "019b7ed4-76a0-7000-8000-000000000001",
        "results": records,
        "provider": provider,
        "warnings": [{"category": "OpenBBWarning", "message": "delayed"}],
        "extra": {"metadata": {"source": "official"}},
        "chart": {"ignored_but_tolerated": True},
    }


def test_fetch_uses_fixed_route_and_preserves_openbb_metadata() -> None:
    seen: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        seen.append(incoming)
        return httpx.Response(200, json=response_payload(), request=incoming)

    credentials = RecordingCredentialProvider()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(
            client=client,
            credentials=credentials,
            clock=lambda: NOW,
        )
        observation = adapter.fetch(
            request(start_date="2026-01-01", end_date="2026-01-02")
        )

    assert observation.state is ProviderDataState.AVAILABLE
    assert observation.completeness == 1
    assert len(observation.data) == 1
    price = observation.data[0]
    assert price.bar.close == 104
    assert price.bar.timeline.event_time == datetime(2026, 1, 2, tzinfo=UTC)
    assert price.bar.timeline.available_at == NOW
    assert price.bar.timeline.as_of == NOW
    assert price.provider_record.model_extra == {
        "future_provider_field": {"kept": True}
    }
    assert observation.metadata is not None
    assert observation.metadata.id == "019b7ed4-76a0-7000-8000-000000000001"
    assert observation.metadata.provider == "yfinance"
    assert observation.metadata.warnings[0].message == "delayed"
    assert observation.metadata.extra["metadata"] == {"source": "official"}
    assert len(seen) == 1
    assert seen[0].url == httpx.URL(
        "http://127.0.0.1:6900/api/v1/equity/price/historical"
        "?symbol=AAPL&start_date=2026-01-01&end_date=2026-01-02&provider=yfinance"
    )
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    request_hash = canonical_request_hash(
        {
            "method": "GET",
            "path": ENDPOINT,
            "query": {
                "symbol": "AAPL",
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
                "provider": "yfinance",
            },
        }
    )
    assert credentials.calls == [
        ServiceCredentialRequest(
            receiver=ServiceReceiver.OPENBB,
            permission=Permission.DISPATCH_ASSIGNED_MARKET_DATA,
            target=AccessTarget(kind=ResourceKind.MARKET, identifier="US/AAPL"),
            request_id=None,
            run_id=None,
            attempt_generation=0,
            attempt_nonce_hash=request_hash,
            request_hash=request_hash,
            expires_no_later_than=NOW + timedelta(seconds=10),
        )
    ]


def test_common_daily_policy_query_ignores_non_openbb_routing_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        seen.append(incoming)
        return httpx.Response(200, json=response_payload(), request=incoming)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(
            request(
                interval="1d",
                scenario="canonical",
                start_date="2026-01-01",
                end_date="2026-01-02",
            )
        )

    assert observation.state is ProviderDataState.AVAILABLE
    assert len(seen) == 1
    assert dict(seen[0].url.params) == {
        "symbol": "AAPL",
        "start_date": "2026-01-01",
        "end_date": "2026-01-02",
        "provider": "yfinance",
    }


def test_unsupported_capability_is_not_a_fetch_failure() -> None:
    calls = 0

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response_payload(), request=incoming)

    unsupported = FetchDataRequest(
        market="HK",
        capability="prices",
        as_of=NOW,
        query={"symbol": "0700"},
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(unsupported)

    assert observation.state is ProviderDataState.NOT_SUPPORTED
    assert observation.reasons == ("openbb_capability_not_supported",)
    assert calls == 0


@pytest.mark.parametrize(
    ("results", "reason"),
    [
        (
            [
                {
                    "date": "2026-01-03",
                    "open": 100,
                    "high": 105,
                    "low": 99,
                    "close": 104,
                    "volume": 1,
                }
            ],
            "openbb_future_data",
        ),
        (
            [
                {
                    "date": "2026-01-02",
                    "open": 100,
                    "high": 105,
                    "low": 99,
                    "close": 104,
                    "volume": 1,
                },
                {
                    "date": "2026-01-02",
                    "open": 100,
                    "high": 105,
                    "low": 99,
                    "close": 104,
                    "volume": 1,
                },
            ],
            "openbb_duplicate_time",
        ),
        (
            [
                {
                    "date": "2026-01-02",
                    "open": 98,
                    "high": 105,
                    "low": 99,
                    "close": 104,
                    "volume": 1,
                }
            ],
            "openbb_conflicting_data",
        ),
    ],
)
def test_future_duplicate_and_invalid_ohlc_are_conflicts(
    results: list[dict[str, object]],
    reason: str,
) -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_payload(results=results),
            request=incoming,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(request())

    assert observation.state is ProviderDataState.CONFLICT
    assert observation.data == ()
    assert observation.reasons == (reason,)


def test_historical_as_of_cannot_use_a_later_live_observation() -> None:
    calls = 0

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response_payload(), request=incoming)

    historical = request().model_copy(
        update={"as_of": datetime(2026, 1, 1, 21, tzinfo=UTC)}
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(historical)

    assert observation.state is ProviderDataState.CONFLICT
    assert observation.reasons == ("openbb_point_in_time_unproven",)
    assert calls == 0


def test_empty_openbb_results_are_legitimate_empty_with_metadata() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_payload(results=[]),
            request=incoming,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(request())

    assert observation.state is ProviderDataState.LEGITIMATE_EMPTY
    assert observation.data == ()
    assert observation.metadata is not None
    assert observation.metadata.provider == "yfinance"


def test_nullable_openbb_warnings_normalize_to_empty_tuple() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = response_payload()
        payload["warnings"] = None
        return httpx.Response(200, json=payload, request=incoming)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(request())

    assert observation.state is ProviderDataState.AVAILABLE
    assert observation.metadata is not None
    assert observation.metadata.warnings == ()


def test_nullable_openbb_extra_normalizes_to_empty_mapping() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = response_payload()
        payload["extra"] = None
        return httpx.Response(200, json=payload, request=incoming)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(request())

    assert observation.state is ProviderDataState.AVAILABLE
    assert observation.metadata is not None
    assert observation.metadata.extra == {}


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ({"provider": "fmp"}, "disallowed_query_parameter"),
        (
            {"url": "http://169.254.169.254/latest/meta-data"},
            "disallowed_query_parameter",
        ),
        ({"symbol": "../../etc/passwd"}, "invalid_symbol"),
        ({"start_date": "not-a-date"}, "invalid_start_date"),
        (
            {"start_date": "2026-01-03", "end_date": "2026-01-02"},
            "invalid_date_range",
        ),
    ],
)
def test_request_injection_fails_closed_without_network_call(
    query: dict[str, object],
    reason: str,
) -> None:
    calls = 0

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response_payload(), request=incoming)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(request(**query))

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == (f"openbb_invalid_request:{reason}",)
    assert calls == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"origin": "http://169.254.169.254"},
            "OpenBB origin is not allowlisted",
        ),
        (
            {"endpoint": "//evil.example/equity/price/historical"},
            "OpenBB endpoint is not allowlisted",
        ),
        ({"provider": "fmp"}, "OpenBB provider is not allowlisted"),
    ],
)
def test_adapter_configuration_is_allowlisted(
    kwargs: dict[str, str],
    message: str,
) -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        ) as client,
        pytest.raises(ValueError, match=message),
    ):
        _adapter(client=client, **kwargs)


def test_unavailable_sidecar_returns_typed_failure_without_leaking_error() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("credential=should-not-leak", request=incoming)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(request())

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.data == ()
    assert observation.reasons == ("openbb_unavailable",)
    assert "credential" not in repr(observation)


def test_missing_service_credential_fails_before_network_without_leaking() -> None:
    calls = 0

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response_payload(), request=incoming)

    unavailable = Failure(
        StructuredError(
            code=ErrorCode.UNAUTHORIZED,
            message="token=must-not-leak",
        )
    )
    credentials = RecordingCredentialProvider(unavailable)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(
            client=client,
            credentials=credentials,
            clock=lambda: NOW,
        ).fetch(request())

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("openbb_service_credential_unavailable",)
    assert calls == 0
    assert "must-not-leak" not in observation.model_dump_json()


@pytest.mark.parametrize(
    ("status_code", "state"),
    [
        (301, ProviderDataState.FETCH_FAILED),
        (307, ProviderDataState.FETCH_FAILED),
        (401, ProviderDataState.FETCH_FAILED),
        (402, ProviderDataState.QUOTA_EXHAUSTED),
        (404, ProviderDataState.NOT_SUPPORTED),
        (429, ProviderDataState.QUOTA_EXHAUSTED),
        (500, ProviderDataState.FETCH_FAILED),
        (503, ProviderDataState.FETCH_FAILED),
    ],
)
def test_redirects_and_http_errors_are_typed_failures(
    status_code: int,
    state: ProviderDataState,
) -> None:
    calls = 0

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
            request=incoming,
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(request())

    assert observation.state is state
    assert observation.reasons == (f"openbb_http_status:{status_code}",)
    assert calls == 1


@pytest.mark.parametrize(
    "content_length",
    ["1048577", "invalid", "-1", "9" * 5000],
)
def test_invalid_or_oversized_content_length_fails_before_streaming(
    content_length: str,
) -> None:
    class NeverRead(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise AssertionError("oversized response body must not be consumed")

    def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-length": content_length,
            },
            stream=NeverRead(),
            request=incoming,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(request())

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("openbb_invalid_response",)


def test_non_identity_content_encoding_is_rejected_without_decompression() -> None:
    class NeverRead(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise AssertionError("encoded response body must not be consumed")

    def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
            stream=NeverRead(),
            request=incoming,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(request())

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("openbb_invalid_response",)


def test_total_response_deadline_stops_slow_chunk_stream() -> None:
    payload = json.dumps(response_payload()).encode()

    class ChunkedBody(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            midpoint = len(payload) // 2
            yield payload[:midpoint]
            yield payload[midpoint:]

    ticks: deque[float] = deque([0.0, 0.25, 1.25])

    def monotonic_clock() -> float:
        return ticks.popleft() if len(ticks) > 1 else ticks[0]

    def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=ChunkedBody(),
            request=incoming,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(
            client=client,
            timeout_seconds=1.0,
            clock=lambda: NOW,
            monotonic_clock=monotonic_clock,
        )
        observation = adapter.fetch(request())

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("openbb_unavailable",)


@pytest.mark.parametrize(
    "make_response",
    [
        lambda incoming: httpx.Response(
            200,
            content=b"not-json",
            headers={"content-type": "application/json"},
            request=incoming,
        ),
        lambda incoming: httpx.Response(
            200,
            content=b"<html>not json</html>",
            headers={"content-type": "text/html"},
            request=incoming,
        ),
        lambda incoming: httpx.Response(
            200,
            json=response_payload(provider="unexpected"),
            request=incoming,
        ),
        lambda incoming: httpx.Response(
            200,
            json={**response_payload(), "results": None},
            request=incoming,
        ),
    ],
)
def test_schema_content_type_and_provider_drift_fail_closed(
    make_response: object,
) -> None:
    assert callable(make_response)
    transport = httpx.MockTransport(make_response)
    with httpx.Client(transport=transport) as client:
        observation = _adapter(client=client, clock=lambda: NOW).fetch(request())

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.reasons == ("openbb_invalid_response",)
