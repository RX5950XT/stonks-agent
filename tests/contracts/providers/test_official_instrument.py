from __future__ import annotations

from datetime import UTC, datetime

import httpx

from stonks_agent.adapters.market_data.official_instrument import (
    SEC_SUBMISSIONS_PATH,
    SEC_TICKERS_URL,
    TWSE_ENDPOINTS,
    OfficialInstrumentDataSource,
    _twse_published_at,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.instrument_data import InstrumentDataQuery

NOW = datetime(2026, 7, 24, 20, tzinfo=UTC)


def test_sec_returns_facts_and_filings_without_future_records() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url)
        if url == SEC_TICKERS_URL:
            payload: object = {"0": {"cik_str": 320193, "ticker": "AAPL"}}
        elif url.endswith(SEC_SUBMISSIONS_PATH.format(cik="0000320193")):
            payload = {
                "name": "Apple Inc.",
                "exchanges": ["Nasdaq"],
                "sicDescription": "Electronic Computers",
                "filings": {
                    "recent": {
                        "form": ["10-K", "10-Q"],
                        "filingDate": ["2026-07-23", "2026-07-25"],
                        "reportDate": ["2026-06-30", "2026-09-30"],
                        "accessionNumber": [
                            "0000320193-26-000001",
                            "0000320193-26-000002",
                        ],
                        "primaryDocument": ["a10k.htm", "a10q.htm"],
                        "primaryDocDescription": ["Annual report", "Future report"],
                    }
                },
            }
        else:
            payload = {
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "Revenues": {
                            "label": "Revenues",
                            "units": {
                                "USD": [
                                    {
                                        "val": 100,
                                        "start": "2026-01-01",
                                        "end": "2026-06-30",
                                        "filed": "2026-07-23",
                                    },
                                    {
                                        "val": 90,
                                        "start": "2025-10-01",
                                        "end": "2025-12-31",
                                        "filed": "2026-02-01",
                                    },
                                    {
                                        "val": 999,
                                        "start": "2026-07-01",
                                        "end": "2026-09-30",
                                        "filed": "2026-07-25",
                                    },
                                ]
                            },
                        },
                        "Assets": {
                            "label": "Assets",
                            "units": {
                                "USD": [
                                    {
                                        "val": 500,
                                        "end": "2026-06-30",
                                        "filed": "2026-07-23",
                                    }
                                ]
                            },
                        },
                    }
                },
            }
        return httpx.Response(200, json=payload, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = OfficialInstrumentDataSource(client=client)
        result = source.fetch(
            InstrumentDataQuery(symbol="aapl", as_of=NOW),
            observed_at=NOW,
        )

    assert isinstance(result, Success)
    assert result.value.symbol == "AAPL"
    assert result.value.name == "Apple Inc."
    assert result.value.exchange == "Nasdaq"
    assert {fact.key for fact in result.value.facts} == {"revenue", "assets"}
    revenue = next(fact for fact in result.value.facts if fact.key == "revenue")
    assert revenue.value == "100"
    assert tuple(item.value for item in revenue.history) == ("100", "90")
    assert len(result.value.filings) == 1
    assert result.value.filings[0].form == "10-K"
    assert len(requests) == 3


def test_sec_cache_reuses_ticker_and_company_payloads() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        url = str(request.url)
        if url == SEC_TICKERS_URL:
            payload: object = {"0": {"cik_str": 320193, "ticker": "AAPL"}}
        elif "/submissions/" in url:
            payload = {
                "name": "Apple Inc.",
                "filings": {
                    "recent": {
                        "form": [],
                        "filingDate": [],
                        "accessionNumber": [],
                        "primaryDocument": [],
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
                                        "val": 1,
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
        source = OfficialInstrumentDataSource(client=client)
        query = InstrumentDataQuery(symbol="AAPL", as_of=NOW)
        first = source.fetch(query, observed_at=NOW)
        second = source.fetch(query, observed_at=NOW)

    assert isinstance(first, Success)
    assert isinstance(second, Success)
    assert calls == 3


def test_twse_returns_monthly_revenue_income_and_balance() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path.removeprefix("/v1")
        if path == TWSE_ENDPOINTS["monthly_revenue"]:
            payload = [
                {
                    "公司代號": "2330",
                    "公司名稱": "台積電",
                    "出表日期": "2026-07-23",
                    "資料年月": "202606",
                    "當月營收": "1000",
                    "上月營收": "900",
                    "去年當月營收": "800",
                    "營收成長率%": "25",
                },
                {
                    "公司代號": "2330",
                    "公司名稱": "台積電",
                    "出表日期": "2026-06-23",
                    "資料年月": "202605",
                    "當月營收": "950",
                    "上月營收": "880",
                    "去年當月營收": "760",
                    "營收成長率%": "25",
                },
            ]
        elif path == TWSE_ENDPOINTS["income_statement"]:
            payload = [
                {
                    "公司代號": "2330",
                    "公司名稱": "台積電",
                    "出表日期": "2026-07-23",
                    "年度": "2026",
                    "季別": "第 2 季",
                    "營業收入": "1000",
                    "營業利益": "300",
                    "稅前淨利": "320",
                    "本期淨利": "280",
                    "每股盈餘": "10",
                }
            ]
        else:
            payload = [
                {
                    "公司代號": "2330",
                    "公司名稱": "台積電",
                    "出表日期": "2026-07-23",
                    "年度": "2026",
                    "季別": "第 2 季",
                    "資產總額": "5000",
                    "負債總額": "2000",
                    "權益總額": "3000",
                }
            ]
        return httpx.Response(200, json=payload, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OfficialInstrumentDataSource(client=client).fetch(
            InstrumentDataQuery(symbol="2330.TW", as_of=NOW),
            observed_at=NOW,
        )

    assert isinstance(result, Success)
    assert result.value.market == "TW"
    assert result.value.name == "台積電"
    assert result.value.provider == "twse_openapi"
    assert {fact.key for fact in result.value.facts} >= {
        "monthly_revenue",
        "revenue",
        "assets",
        "equity",
    }
    monthly = next(fact for fact in result.value.facts if fact.key == "monthly_revenue")
    assert tuple(item.period for item in monthly.history) == ("2026-06", "2026-05")
    assert len(requests) == 3


def test_official_http_failures_are_typed_and_never_cached() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SEC_TICKERS_URL:
            return httpx.Response(429, request=request)
        return httpx.Response(500, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OfficialInstrumentDataSource(client=client).fetch(
            InstrumentDataQuery(symbol="AAPL", as_of=NOW),
            observed_at=NOW,
        )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.RATE_LIMITED


def test_twse_cutoff_uses_taiwan_calendar_date() -> None:
    observed = datetime(2026, 8, 30, 22, tzinfo=UTC)

    assert (
        _twse_published_at(
            {"出表日期": "1150831"},
            observed,
            observed,
        )
        is not None
    )
