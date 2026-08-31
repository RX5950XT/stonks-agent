from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from stonks_agent.adapters.market_data.instrument_bundle import (
    InstrumentResearchSnapshotSource,
)
from stonks_agent.adapters.market_data.official_instrument import (
    SEC_TICKERS_URL,
    OfficialInstrumentDataSource,
)
from stonks_agent.adapters.market_data.openbb_rest import OpenBBRestAdapter
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.errors import Success
from stonks_agent.ports.service_credentials import (
    ServiceBearerCredential,
    ServiceCredentialRequest,
)

NOW = datetime(2026, 7, 24, 20, tzinfo=UTC)


class Credentials:
    def issue(
        self,
        request: ServiceCredentialRequest,
    ) -> Success[ServiceBearerCredential]:
        del request
        return Success(ServiceBearerCredential(token=SecretStr("x" * 32)))


def test_research_bundle_contains_market_fundamental_and_filing_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SEC_TICKERS_URL:
            payload: object = {"0": {"cik_str": 320193, "ticker": "AAPL"}}
        elif request.url.host == "127.0.0.1":
            payload = {
                "id": "openbb-1",
                "provider": "yfinance",
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
            }
        elif request.url.path.endswith("/submissions/CIK0000320193.json"):
            payload = {
                "name": "Apple Inc.",
                "filings": {
                    "recent": {
                        "form": ["10-K"],
                        "filingDate": ["2026-07-23"],
                        "reportDate": ["2026-06-30"],
                        "accessionNumber": ["0000320193-26-000001"],
                        "primaryDocument": ["a10k.htm"],
                    }
                },
            }
        else:
            payload = {
                "facts": {
                    "us-gaap": {
                        "Assets": {
                            "units": {
                                "USD": [
                                    {
                                        "val": 500,
                                        "end": "2026-06-30",
                                        "filed": "2026-07-23",
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        return httpx.Response(200, json=payload, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = InstrumentResearchSnapshotSource(
            market=OpenBBRestAdapter(
                client=client,
                credentials=Credentials(),
                clock=lambda: NOW,
            ),
            instrument=OfficialInstrumentDataSource(client=client),
            clock=lambda: NOW,
        )
        result = source.fetch(
            FetchDataRequest(
                market="US",
                capability="research_data",
                as_of=NOW,
                query={
                    "symbol": "AAPL",
                    "start_date": "2026-07-23",
                    "end_date": "2026-07-24",
                    "interval": "1d",
                },
            ),
            provider_policy_id="us-research/1",
        )

    assert isinstance(result, Success)
    assert result.value.provider == "stonks_bundle"
    assert {item.kind.value for item in result.value.evidence} == {
        "market_data",
        "fundamental",
        "filing",
    }
    assert result.value.observation.state.value == "available"
    assert result.value.raw_payload.startswith(b'{"bundle_version"')
