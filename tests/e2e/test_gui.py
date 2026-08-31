from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.gui_model_settings import (
    ConfigureGuiModelSettings,
    GuiModelSettingsView,
)
from stonks_agent.domain.gui_paper import (
    GuiPaperCashView,
    GuiPaperIntegrityView,
    GuiPaperNavView,
    GuiPaperPortfolioView,
    GuiPaperRiskView,
    GuiPaperSafetyView,
)
from stonks_agent.domain.gui_research import (
    GuiKronosAlphaView,
    GuiKronosForecastView,
    GuiResearchClaim,
    GuiResearchCommand,
    GuiResearchEvidenceField,
    GuiResearchEvidenceItem,
    GuiResearchEvidenceView,
    GuiResearchHistoryItem,
    GuiResearchHistoryView,
    GuiResearchIssueView,
    GuiResearchRunRef,
    GuiResearchRunView,
    GuiResearchUsageView,
    GuiResearchVersionView,
)
from stonks_agent.domain.instrument_data import (
    InstrumentDataQuery,
    InstrumentFact,
    InstrumentFiling,
    InstrumentOverview,
)
from stonks_agent.domain.latest_market_data import (
    BarInterval,
    LatestMarketBar,
    LatestMarketDataObservation,
    LatestMarketDataQuery,
)
from stonks_agent.domain.research_run import CanonicalRunEvent
from stonks_agent.entrypoints.api.gui import (
    PaperCapability,
    PaperRow,
    ServiceStatus,
    create_gui_app,
)
from stonks_agent.entrypoints.api.gui_research import GuiResearchApiOptions

NOW = datetime(2026, 7, 24, 20, tzinfo=UTC)
RESEARCH_RUN_ID = UUID("8a000000-0000-4000-8000-000000000001")


class Source:
    def __init__(
        self,
        unavailable: bool = False,
        *,
        warnings: tuple[str, ...] = ("delayed",),
        failing_symbols: frozenset[str] = frozenset(),
    ) -> None:
        self.unavailable = unavailable
        self.warnings = warnings
        self.failing_symbols = failing_symbols
        self.calls: list[LatestMarketDataQuery] = []

    def fetch(
        self,
        query: LatestMarketDataQuery,
        *,
        observed_at: datetime,
    ) -> Success[LatestMarketDataObservation] | Failure:
        self.calls.append(query)
        if self.unavailable or query.symbol in self.failing_symbols:
            return Failure(
                StructuredError(
                    code=ErrorCode.DATA_UNAVAILABLE,
                    message="Latest market data is unavailable",
                )
            )
        return Success(
            LatestMarketDataObservation(
                symbol=query.symbol,
                provider="openbb:yfinance",
                feed_type=query.interval.feed_type,
                interval=query.interval,
                observed_at=observed_at,
                bars=(
                    bar("2026-07-23T00:00:00Z", "188.00"),
                    bar("2026-07-24T00:00:00Z", "191.200000000001"),
                ),
                warnings=self.warnings,
            )
        )


class InstrumentSource:
    def fetch(
        self,
        query: InstrumentDataQuery,
        *,
        observed_at: datetime,
    ) -> Success[InstrumentOverview]:
        return Success(
            InstrumentOverview(
                symbol=query.symbol,
                market="US",
                name="Apple Inc.",
                exchange="NASDAQ",
                industry="Technology",
                cik="0000320193",
                state="available",
                provider="sec",
                observed_at=observed_at,
                as_of=query.as_of,
                facts=(
                    InstrumentFact(
                        key="revenue",
                        label="營收",
                        value="100",
                        unit="USD",
                        period="2026-06-30",
                        event_time=observed_at,
                        published_at=observed_at,
                        available_at=observed_at,
                        provider="sec",
                        source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
                    ),
                ),
                filings=(
                    InstrumentFiling(
                        form="10-Q",
                        filed_at=observed_at,
                        period_end=observed_at,
                        description="Quarterly report",
                        provider="sec",
                        source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/a10q.htm",
                    ),
                ),
            )
        )


class ResearchFacade:
    def __init__(self, *, status: str = "succeeded") -> None:
        self.status = status
        self.submissions: list[tuple[LocalPrincipal, GuiResearchCommand]] = []
        self.event_queries: list[tuple[LocalPrincipal, UUID, int, int]] = []
        self.history_queries: list[tuple[LocalPrincipal, int]] = []

    def submit(
        self,
        principal: LocalPrincipal,
        command: GuiResearchCommand,
    ) -> Success[GuiResearchRunRef]:
        self.submissions.append((principal, command))
        return Success(GuiResearchRunRef(run_id=RESEARCH_RUN_ID))

    def read(
        self,
        principal: LocalPrincipal,
        run_id: UUID,
    ) -> Success[GuiResearchRunView]:
        del principal
        return Success(
            GuiResearchRunView(
                run_id=run_id,
                symbol="AAPL",
                status=self.status,
                stage="report",
                claims=(
                    GuiResearchClaim(
                        text="Evidence-backed claim",
                        evidence_ids=(UUID("8a000000-0000-4000-8000-000000000010"),),
                    ),
                ),
                counterarguments=("Valuation remains elevated.",),
                risks=("Provider publication lag.",),
                confidence=Decimal("0.72"),
                as_of=NOW,
                snapshot_id=UUID("8a000000-0000-4000-8000-000000000030"),
                evidence_count=1,
                usage=GuiResearchUsageView(
                    iterations=2,
                    tool_calls=1,
                    input_tokens=100,
                    output_tokens=20,
                    cost_usd=Decimal("0.01"),
                    elapsed_ms=250,
                ),
                issues=(
                    GuiResearchIssueView(
                        stage="tradingagents",
                        code="tradingagents_unavailable",
                    ),
                ),
                warnings=("provider publication lag",),
                versions=(
                    GuiResearchVersionView(
                        component="model:1",
                        version="custom-model",
                    ),
                ),
                kronos_forecast=GuiKronosForecastView(
                    state="succeeded",
                    actual_model_inference=True,
                    forecast_id=UUID("8a000000-0000-4000-8000-000000000011"),
                    model_id="NeoQuasar/Kronos-small",
                    model_revision="9" * 40,
                    generated_at=NOW,
                    horizon_bars=1,
                    path_count=3,
                    expected_return=Decimal("0.012"),
                    median_return=Decimal("0.01"),
                    direction_probability=Decimal("0.667"),
                    expected_volatility=Decimal("0.02"),
                    downside_quantile=Decimal("-0.03"),
                    max_drawdown_quantile=Decimal("-0.04"),
                    quality_status="available",
                ),
                kronos_alpha=GuiKronosAlphaView(
                    state="blocked",
                    deployment_state="shadow",
                    eligible=False,
                    weight=Decimal(0),
                    reason_codes=("strategy_not_paper_eligible",),
                ),
                paper_decision="no-order: strategy_not_paper_eligible",
                report_content="# AAPL research report",
                updated_at=NOW,
            )
        )

    def recent(
        self,
        principal: LocalPrincipal,
        *,
        limit: int,
    ) -> Success[GuiResearchHistoryView]:
        self.history_queries.append((principal, limit))
        return Success(
            GuiResearchHistoryView(
                items=(
                    GuiResearchHistoryItem(
                        run_id=RESEARCH_RUN_ID,
                        symbol="AAPL",
                        profile="balanced/1",
                        status="succeeded",
                        stage="report",
                        as_of=NOW,
                        confidence=Decimal("0.72"),
                        updated_at=NOW,
                    ),
                )
            )
        )

    def evidence(
        self,
        principal: LocalPrincipal,
        run_id: UUID,
    ) -> Success[GuiResearchEvidenceView]:
        del principal
        return Success(
            GuiResearchEvidenceView(
                run_id=run_id,
                items=(
                    GuiResearchEvidenceItem(
                        evidence_id=UUID("8a000000-0000-4000-8000-000000000010"),
                        kind="market_data",
                        source="openbb:yfinance",
                        provider="yfinance",
                        event_time=NOW,
                        available_at=NOW,
                        quality_status="available",
                        completeness=Decimal("1"),
                        content_hash="d" * 64,
                        fields=(
                            GuiResearchEvidenceField(
                                name="close",
                                value="191.20",
                            ),
                        ),
                    ),
                ),
            )
        )

    def events(
        self,
        principal: LocalPrincipal,
        run_id: UUID,
        *,
        after_sequence: int,
        limit: int,
    ) -> Success[tuple[CanonicalRunEvent, ...]]:
        self.event_queries.append((principal, run_id, after_sequence, limit))
        return Success(
            (
                CanonicalRunEvent(
                    event_id=UUID("8a000000-0000-4000-8000-000000000020"),
                    run_id=run_id,
                    sequence=2,
                    event_type="research.running",
                    payload={"stage": "research", "api_key": "must-redact"},
                    occurred_at=NOW,
                    event_hash="a" * 64,
                ),
                CanonicalRunEvent(
                    event_id=UUID("8a000000-0000-4000-8000-000000000021"),
                    run_id=run_id,
                    sequence=3,
                    event_type="research.succeeded",
                    payload={"stage": "report"},
                    occurred_at=NOW,
                    event_hash="b" * 64,
                ),
                CanonicalRunEvent(
                    event_id=UUID("8a000000-0000-4000-8000-000000000022"),
                    run_id=run_id,
                    sequence=4,
                    event_type="research.running",
                    payload={"stage": "forbidden-after-terminal"},
                    occurred_at=NOW,
                    event_hash="c" * 64,
                ),
            )
        )


class UnconfiguredModelSettings:
    def view(self) -> GuiModelSettingsView:
        return GuiModelSettingsView(
            state="unconfigured",
            source="none",
            detail="Model connection is not configured.",
            api_key_configured=False,
            verified=False,
            generation=0,
        )

    def configure(self, command: ConfigureGuiModelSettings) -> Failure:
        del command
        return Failure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="Not configured",
            )
        )

    def clear(self) -> Success[GuiModelSettingsView]:
        return Success(self.view())


def bar(event_time: str, close: str) -> LatestMarketBar:
    return LatestMarketBar(
        event_time=event_time,
        open="188",
        high="193",
        low="187",
        close=close,
        volume="1000000",
    )


def client(source: Source | None = None, **kwargs: object) -> TestClient:
    app = create_gui_app(source or Source(), clock=lambda: NOW, **kwargs)  # type: ignore[arg-type]
    return TestClient(
        app,
        base_url="http://127.0.0.1:8787",
        client=("127.0.0.1", 50_000),
    )


def research_options() -> GuiResearchApiOptions:
    return GuiResearchApiOptions(
        account_id="paper-local",
        allowed_profiles=("balanced/1",),
        default_profile="balanced/1",
        intent_token="intent-" + "a" * 32,
    )


def research_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": "http://127.0.0.1:8787",
        "X-Stonks-Intent": "intent-" + "a" * 32,
    }


@pytest.mark.parametrize(
    "overrides",
    (
        {"account_id": "../unsafe"},
        {"allowed_profiles": ("balanced/1", "balanced/1")},
        {"intent_token": "too-short"},
    ),
)
def test_research_api_options_reject_unsafe_server_configuration(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        GuiResearchApiOptions(**overrides)  # type: ignore[arg-type]


def test_gui_rejects_an_object_that_is_not_a_research_facade() -> None:
    with pytest.raises(TypeError):
        create_gui_app(Source(), research=object())  # type: ignore[arg-type]


def test_console_shell_is_static_and_loads_only_same_origin_assets() -> None:
    with client() as browser:
        response = browser.get("/")
        script = browser.get("/assets/terminal.js")
        style = browser.get("/assets/terminal.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Stonks Desk" in response.text
    assert "AI 投資研究工作台" in response.text
    assert "僅模擬交易 · 研究需驗證 · 僅本機" in response.text
    assert 'id="market-search"' in response.text
    assert 'id="symbol-search"' in response.text
    assert 'id="research-action"' in response.text
    assert 'id="research-progress"' in response.text
    assert 'id="research-results"' in response.text
    assert 'id="paper-body"' in response.text
    assert 'id="panel-overview"' in response.text
    assert 'id="instrument-dashboard"' in response.text
    assert 'id="panel-provenance"' not in response.text
    assert '<script src="/assets/terminal.js" defer></script>' in response.text
    assert '<script src="/assets/product.js" defer></script>' in response.text
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert style.status_code == 200
    assert style.headers["content-type"].startswith("text/css")


def test_console_csp_admits_same_origin_script_and_nothing_else() -> None:
    with client() as browser:
        policy = browser.get("/").headers["content-security-policy"]

    assert "script-src 'self'" in policy
    assert "connect-src 'self'" in policy
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert "https://" not in policy


def test_bars_endpoint_returns_the_series_with_derived_change() -> None:
    with client() as browser:
        response = browser.get("/api/v1/market/bars?symbol=aapl&interval=1d")

    payload = response.json()
    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["data"]["symbol"] == "AAPL"
    assert payload["data"]["is_real_time"] is False
    assert payload["data"]["interval"] == "1d"
    assert payload["data"]["previous_close"] == "188.00"
    assert payload["data"]["change"] == "3.200000000001"
    assert payload["data"]["change_percent"] == "1.70"
    assert payload["data"]["freshness"] == "unknown"
    assert payload["data"]["quality"] == "degraded"
    assert payload["data"]["quality_reasons"] == ["provider_warning"]
    assert payload["data"]["served_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert payload["data"]["cache_hit"] is False
    assert len(payload["data"]["bars"]) == 2
    assert response.headers["cache-control"] == "no-store"


def test_instrument_endpoint_returns_company_financials_and_filings() -> None:
    with client(instrument_data=InstrumentSource()) as browser:
        response = browser.get("/api/v1/instrument/overview?symbol=aapl")

    payload = response.json()
    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["data"]["symbol"] == "AAPL"
    assert payload["data"]["name"] == "Apple Inc."
    assert payload["data"]["facts"][0]["key"] == "revenue"
    assert payload["data"]["filings"][0]["form"] == "10-Q"


def test_intraday_request_reaches_the_provider_with_its_interval() -> None:
    source = Source()
    with client(source) as browser:
        response = browser.get(
            "/api/v1/market/bars?symbol=NVDA&interval=1h&lookback_days=30"
        )

    assert response.status_code == 200
    assert response.json()["data"]["feed_type"] == "intraday_historical"
    assert source.calls[-1].interval is BarInterval.HOUR
    assert source.calls[-1].lookback_days == 30


@pytest.mark.parametrize(
    ("interval", "lookback_days"),
    (("2m", 7), ("30m", 30), ("90m", 30), ("1W", 30), ("1M", 30), ("1Y", 3_652)),
)
def test_extended_interval_request_reaches_the_provider(
    interval: str, lookback_days: int
) -> None:
    source = Source()
    with client(source) as browser:
        response = browser.get(
            f"/api/v1/market/bars?symbol=NVDA&interval={interval}&lookback_days={lookback_days}"
        )

    assert response.status_code == 200
    assert source.calls[-1].interval.value == interval


def test_watchlist_reports_each_symbol_independently() -> None:
    source = Source(failing_symbols=frozenset({"MSFT"}))
    with client(source) as browser:
        response = browser.get("/api/v1/market/quotes?symbols=AAPL,MSFT")

    quotes = {entry["symbol"]: entry for entry in response.json()["data"]["quotes"]}
    assert response.status_code == 200
    assert quotes["AAPL"]["quote"]["latest"]["close"] == "191.200000000001"
    assert "bars" not in quotes["AAPL"]["quote"]
    assert quotes["MSFT"]["quote"] is None
    assert quotes["MSFT"]["error"]["code"] == "data_unavailable"
    assert all(call.interval is BarInterval.MINUTE for call in source.calls)


def test_watchlist_accepts_more_than_the_previous_twelve_symbol_limit() -> None:
    symbols = tuple(f"S{index}" for index in range(13))
    source = Source(warnings=())
    with client(source) as browser:
        response = browser.get(f"/api/v1/market/quotes?symbols={','.join(symbols)}")

    assert response.status_code == 200
    assert (
        tuple(item["symbol"] for item in response.json()["data"]["quotes"]) == symbols
    )


def test_quote_cache_recomputes_age_and_marks_cached_delivery() -> None:
    source = Source(warnings=())
    moments = [NOW]

    app = create_gui_app(source, clock=lambda: moments[0])
    with TestClient(
        app,
        base_url="http://127.0.0.1:8787",
        client=("127.0.0.1", 50_000),
    ) as browser:
        first = browser.get("/api/v1/market/quotes?symbols=AAPL")
        moments[0] = datetime(2026, 7, 24, 20, 0, 17, tzinfo=UTC)
        second = browser.get("/api/v1/market/quotes?symbols=AAPL")

    first_quote = first.json()["data"]["quotes"][0]["quote"]
    second_quote = second.json()["data"]["quotes"][0]["quote"]
    assert len(source.calls) == 1
    assert first_quote["cache_hit"] is False
    assert second_quote["cache_hit"] is True
    assert second_quote["observed_at"] == first_quote["observed_at"]
    assert second_quote["data_age_seconds"] == first_quote["data_age_seconds"] + 17


def test_provider_request_budget_bounds_browser_fan_out() -> None:
    source = Source(warnings=())
    with client(source) as browser:
        responses = tuple(
            browser.get(
                f"/api/v1/market/bars?symbol=S{index}&interval=1m&lookback_days=7"
            )
            for index in range(31)
        )

    assert all(response.status_code == 200 for response in responses[:30])
    assert responses[-1].status_code == 429
    assert responses[-1].json()["error"]["code"] == "rate_limited"
    assert len(source.calls) == 30


@pytest.mark.parametrize(
    "query",
    (
        "/api/v1/market/bars?symbol=AAPL&symbol=MSFT",
        "/api/v1/market/bars?symbol=AAPL&provider=fixture",
        "/api/v1/market/bars?symbol=AAPL&interval=3m",
        "/api/v1/market/bars?symbol=%3Cscript%3E",
        "/api/v1/market/bars?symbol=AAPL&interval=1m&lookback_days=30",
        "/api/v1/market/quotes?symbols=AAPL,AAPL",
        "/api/v1/market/quotes?symbols=" + "S" * 4097,
        "/api/v1/capabilities?debug=1",
    ),
)
def test_invalid_or_ambiguous_queries_fail_closed(query: str) -> None:
    with client() as browser:
        response = browser.get(query)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"
    assert "script" not in response.text
    assert "fixture" not in response.text


def test_capabilities_reports_paper_as_unavailable_without_inventing_numbers() -> None:
    with client() as browser:
        response = browser.get("/api/v1/capabilities")

    paper = response.json()["data"]["paper"]
    assert response.status_code == 200
    assert paper["state"] == "unavailable"
    assert paper["rows"] == []
    assert paper["account_id"] is None
    research = response.json()["data"]["research"]
    assert research["state"] == "unavailable"
    assert research["allowed_profiles"] == ["balanced/1"]
    assert len(research["intent_token"]) >= 32


def test_capabilities_surfaces_a_composed_paper_reader() -> None:
    capability = PaperCapability(
        state="ready",
        detail="PostgreSQL paper projections",
        account_id="paper-local",
        rows=(PaperRow(label="NAV", value="100000.00 USD"),),
        portfolio=GuiPaperPortfolioView(
            base_currency="USD",
            as_of=NOW,
            cash=(
                GuiPaperCashView(
                    currency="USD",
                    settled=Decimal("100000"),
                    reserved=Decimal(0),
                    available=Decimal("100000"),
                ),
            ),
            positions=(),
            position_count=0,
            pending_order_count=0,
            latest_target=False,
        ),
        nav=GuiPaperNavView(
            state="available",
            as_of=NOW,
            base_currency="USD",
            nav=Decimal("100000"),
            cash_value=Decimal("100000"),
            position_value=Decimal(0),
            cumulative_fees=Decimal(0),
            realized_pnl=Decimal(0),
        ),
        risk=GuiPaperRiskView(state="empty"),
        integrity=GuiPaperIntegrityView(
            state="verified",
            account_sequence=0,
            portfolio_sequence=0,
            ledger_sequence=0,
            projection_hash="c" * 64,
        ),
        safety=GuiPaperSafetyView(
            state="available",
            active=False,
            reason_code="bootstrap_inactive",
            version=1,
            updated_at=NOW,
        ),
    )
    services = (ServiceStatus(name="openbb", detail="healthy", state="ready"),)

    with client(paper=lambda: capability, services=services) as browser:
        payload = browser.get("/api/v1/capabilities").json()["data"]

    assert payload["paper"]["state"] == "ready"
    assert payload["paper"]["rows"][0]["value"] == "100000.00 USD"
    assert payload["paper"]["portfolio"]["cash"][0]["available"] == "100000"
    assert payload["paper"]["risk"]["state"] == "empty"
    assert payload["paper"]["safety"]["active"] is False
    assert payload["services"][0]["state"] == "ready"


def test_capabilities_reprobes_service_health_instead_of_freezing_startup_state() -> (
    None
):
    state = {"value": "ready"}

    def services() -> tuple[ServiceStatus, ...]:
        return (
            ServiceStatus(
                name="openbb",
                detail="live probe",
                state=state["value"],
            ),
        )

    with client(services=services) as browser:
        first = browser.get("/api/v1/capabilities").json()["data"]["services"]
        state["value"] = "failed"
        second = browser.get("/api/v1/capabilities").json()["data"]["services"]

    assert first[0]["state"] == "ready"
    assert second[0]["state"] == "failed"


def test_a_failing_paper_reader_degrades_instead_of_leaking() -> None:
    def explode() -> PaperCapability:
        raise RuntimeError("connection to postgres://user:secret@host failed")

    with client(paper=explode) as browser:
        response = browser.get("/api/v1/capabilities")

    assert response.status_code == 200
    assert response.json()["data"]["paper"]["state"] == "unavailable"
    assert "secret" not in response.text


def test_provider_outage_fails_closed_and_mutations_do_not_exist() -> None:
    with client(Source(unavailable=True)) as browser:
        unavailable = browser.get("/api/v1/market/bars?symbol=AAPL")
        mutation = browser.post("/v1/paper/kill-switches/activate", json={})

    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "data_unavailable"
    assert mutation.status_code == 404


def test_console_surface_limits_mutations_to_research_and_session_model_settings() -> (
    None
):
    app = create_gui_app(Source(), clock=lambda: NOW)
    assert all(isinstance(route, APIRoute) for route in app.routes)
    assert not any(isinstance(route, WebSocketRoute) for route in app.routes)
    routes: dict[str, set[str]] = {}
    for route in app.routes:
        routes.setdefault(route.path, set()).update(route.methods)

    assert routes == {
        "/": {"GET"},
        "/assets/favicon.svg": {"GET"},
        "/assets/market-data.js": {"GET"},
        "/assets/product.css": {"GET"},
        "/assets/product.js": {"GET"},
        "/assets/research.js": {"GET"},
        "/assets/settings.js": {"GET"},
        "/assets/terminal.css": {"GET"},
        "/assets/terminal.js": {"GET"},
        "/api/v1/capabilities": {"GET"},
        "/api/v1/market/bars": {"GET"},
        "/api/v1/market/quotes": {"GET"},
        "/api/v1/market-data/latest": {"GET"},
        "/api/v1/instrument/overview": {"GET"},
        "/api/v1/research/runs": {"GET", "POST"},
        "/api/v1/research/runs/{run_id}": {"GET"},
        "/api/v1/research/runs/{run_id}/evidence": {"GET"},
        "/api/v1/research/runs/{run_id}/events": {"GET"},
        "/api/v1/settings/llm": {"DELETE", "GET", "PUT"},
    }
    assert (
        sum(method == "POST" for methods in routes.values() for method in methods) == 1
    )
    assert (
        sum(method == "PUT" for methods in routes.values() for method in methods) == 1
    )
    assert (
        sum(method == "DELETE" for methods in routes.values() for method in methods)
        == 1
    )
    assert not any(
        token in path
        for path in routes
        for token in ("target", "order", "execution", "risk", "kill-switch")
    )


def test_console_rejects_websocket_scope() -> None:
    with (
        client() as browser,
        pytest.raises(WebSocketDisconnect) as rejected,
        browser.websocket_connect("/ws"),
    ):
        pass

    assert rejected.value.code == 1008


def test_console_rejects_non_loopback_and_forwarded_identity() -> None:
    app = create_gui_app(Source(), clock=lambda: NOW)
    with TestClient(
        app,
        base_url="http://example.test",
        client=("203.0.113.5", 50_000),
    ) as remote:
        remote_response = remote.get("/")
    with client() as browser:
        forwarded = browser.get("/", headers={"X-Forwarded-For": "127.0.0.1"})

    assert remote_response.status_code == 403
    assert forwarded.status_code == 400


def test_untrusted_provider_warning_stays_data_in_json() -> None:
    warning = '<img src=x onerror="alert(1)">'
    with client(Source(warnings=(warning,))) as browser:
        response = browser.get("/api/v1/market/bars?symbol=AAPL")

    assert response.status_code == 200
    assert response.json()["data"]["warnings"] == [warning]
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize(
    "headers",
    (
        {"Origin": "https://attacker.invalid"},
        {"Sec-Fetch-Site": "cross-site"},
        {
            "Origin": "http://127.0.0.1:8787",
            "Sec-Fetch-Site": "same-site",
        },
    ),
)
def test_console_rejects_cross_site_api_get_before_provider(
    headers: dict[str, str],
) -> None:
    source = Source()
    with client(source) as browser:
        response = browser.get(
            "/api/v1/market/bars?symbol=AAPL",
            headers=headers,
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert source.calls == []


def test_console_allows_exact_same_origin_api_get() -> None:
    source = Source()
    with client(source) as browser:
        response = browser.get(
            "/api/v1/market/bars?symbol=AAPL",
            headers={
                "Origin": "http://127.0.0.1:8787",
                "Sec-Fetch-Site": "same-origin",
            },
        )

    assert response.status_code == 200
    assert len(source.calls) == 1


@pytest.mark.parametrize(
    "host",
    (
        "user@127.0.0.1:8787",
        "localhost/path",
        "localhost.",
        "LOCALHOST:8787",
    ),
)
def test_console_rejects_noncanonical_loopback_host(host: str) -> None:
    with client() as browser:
        response = browser.get("/", headers={"Host": host})

    assert response.status_code == 403


def test_research_post_uses_server_owned_authority_and_returns_accepted_ref() -> None:
    facade = ResearchFacade()
    with client(
        research=facade,
        research_api=research_options(),
    ) as browser:
        response = browser.post(
            "/api/v1/research/runs",
            headers=research_headers(),
            json={
                "symbol": "aapl",
                "interval": "1d",
                "profile": "balanced/1",
            },
        )

    assert response.status_code == 202
    assert response.json()["data"]["run_id"] == str(RESEARCH_RUN_ID)
    assert len(facade.submissions) == 1
    principal, command = facade.submissions[0]
    assert principal.subject == "local-console-research"
    assert principal.roles == frozenset({Role.RESEARCHER})
    assert principal.targets
    assert command.symbol == "AAPL"
    assert command.account_id == "paper-local"
    assert command.execution_mode == "paper"
    assert command.requested_at == NOW


def test_research_post_requires_verified_session_model_when_settings_are_composed() -> (
    None
):
    facade = ResearchFacade()
    with client(
        research=facade,
        research_api=research_options(),
        model_settings=UnconfiguredModelSettings(),
    ) as browser:
        response = browser.post(
            "/api/v1/research/runs",
            headers=research_headers(),
            json={
                "symbol": "AAPL",
                "interval": "1d",
                "profile": "balanced/1",
            },
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "data_unavailable"
    assert facade.submissions == []


@pytest.mark.parametrize(
    ("headers", "body"),
    (
        (
            {
                "Content-Type": "application/json",
                "Origin": "https://attacker.invalid",
                "X-Stonks-Intent": "intent-" + "a" * 32,
            },
            {"symbol": "AAPL", "interval": "1d", "profile": "balanced/1"},
        ),
        (
            {
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:8787",
            },
            {"symbol": "AAPL", "interval": "1d", "profile": "balanced/1"},
        ),
        (
            research_headers(),
            {
                "symbol": "AAPL",
                "interval": "1d",
                "profile": "balanced/1",
                "order_side": "buy",
            },
        ),
        (
            research_headers(),
            {"symbol": "AAPL", "interval": "3m", "profile": "balanced/1"},
        ),
        (
            research_headers(),
            {"symbol": "AAPL", "interval": "1d", "profile": "unapproved/1"},
        ),
    ),
)
def test_research_post_rejects_cross_origin_missing_intent_and_untrusted_fields(
    headers: dict[str, str],
    body: dict[str, object],
) -> None:
    facade = ResearchFacade()
    with client(
        research=facade,
        research_api=research_options(),
    ) as browser:
        response = browser.post(
            "/api/v1/research/runs",
            headers=headers,
            json=body,
        )

    assert response.status_code in {400, 403}
    assert facade.submissions == []


def test_research_post_requires_exact_json_content_type() -> None:
    facade = ResearchFacade()
    headers = {
        **research_headers(),
        "Content-Type": "application/json; charset=utf-8",
    }
    with client(
        research=facade,
        research_api=research_options(),
    ) as browser:
        response = browser.post(
            "/api/v1/research/runs",
            headers=headers,
            content='{"symbol":"AAPL","interval":"1d","profile":"balanced/1"}',
        )

    assert response.status_code == 400
    assert facade.submissions == []


def test_research_start_has_a_dedicated_cost_rate_limit() -> None:
    facade = ResearchFacade()
    with client(
        research=facade,
        research_api=research_options(),
    ) as browser:
        responses = tuple(
            browser.post(
                "/api/v1/research/runs",
                headers=research_headers(),
                json={
                    "symbol": "AAPL",
                    "interval": "1d",
                    "profile": "balanced/1",
                },
            )
            for _ in range(4)
        )

    assert tuple(response.status_code for response in responses) == (
        202,
        202,
        202,
        429,
    )
    assert responses[-1].headers["retry-after"] == "60"
    assert responses[-1].json()["error"]["code"] == "rate_limited"
    assert len(facade.submissions) == 3


def test_research_start_rejects_a_second_run_until_the_first_is_terminal() -> None:
    facade = ResearchFacade(status="running")
    payload = {
        "symbol": "AAPL",
        "interval": "1d",
        "profile": "balanced/1",
    }
    with client(
        research=facade,
        research_api=research_options(),
    ) as browser:
        first = browser.post(
            "/api/v1/research/runs",
            headers=research_headers(),
            json=payload,
        )
        second = browser.post(
            "/api/v1/research/runs",
            headers=research_headers(),
            json=payload,
        )
        facade.status = "succeeded"
        third = browser.post(
            "/api/v1/research/runs",
            headers=research_headers(),
            json=payload,
        )

    assert (first.status_code, second.status_code, third.status_code) == (202, 409, 202)
    assert second.json()["error"]["code"] == "conflict"
    assert len(facade.submissions) == 2


def test_uncomposed_research_surface_returns_structured_unavailable() -> None:
    with client(research_api=research_options()) as browser:
        created = browser.post(
            "/api/v1/research/runs",
            headers=research_headers(),
            json={
                "symbol": "AAPL",
                "interval": "1d",
                "profile": "balanced/1",
            },
        )
        detail = browser.get(f"/api/v1/research/runs/{RESEARCH_RUN_ID}")
        events = browser.get(f"/api/v1/research/runs/{RESEARCH_RUN_ID}/events")
        history = browser.get("/api/v1/research/runs?limit=10")
        evidence = browser.get(f"/api/v1/research/runs/{RESEARCH_RUN_ID}/evidence")

    assert created.status_code == 503
    assert detail.status_code == 503
    assert events.status_code == 503
    assert history.status_code == 503
    assert evidence.status_code == 503
    assert created.json()["error"]["code"] == "data_unavailable"


def test_research_detail_and_sse_are_bounded_redacted_and_terminal() -> None:
    facade = ResearchFacade()
    with client(
        research=facade,
        research_api=research_options(),
    ) as browser:
        detail = browser.get(f"/api/v1/research/runs/{RESEARCH_RUN_ID}")
        events = browser.get(
            f"/api/v1/research/runs/{RESEARCH_RUN_ID}/events?limit=10",
            headers={"Last-Event-ID": "1"},
        )

    assert detail.status_code == 200
    assert detail.json()["data"]["claims"][0]["text"] == "Evidence-backed claim"
    assert detail.json()["data"]["usage"]["input_tokens"] == 100
    assert detail.json()["data"]["issues"][0]["code"] == "tradingagents_unavailable"
    assert detail.json()["data"]["snapshot_id"].endswith("0030")
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "event: research.running" in events.text
    assert "event: research.succeeded" in events.text
    assert "forbidden-after-terminal" not in events.text
    assert "must-redact" not in events.text
    assert facade.event_queries[0][2:] == (1, 10)


def test_research_history_and_cited_evidence_are_browser_safe() -> None:
    facade = ResearchFacade()
    with client(
        research=facade,
        research_api=research_options(),
    ) as browser:
        history = browser.get("/api/v1/research/runs?limit=5")
        evidence = browser.get(f"/api/v1/research/runs/{RESEARCH_RUN_ID}/evidence")

    assert history.status_code == 200
    assert history.json()["data"]["items"][0]["symbol"] == "AAPL"
    assert facade.history_queries[0][1] == 5
    assert evidence.status_code == 200
    item = evidence.json()["data"]["items"][0]
    assert item["fields"] == [{"name": "close", "value": "191.20"}]
    assert "raw_artifact" not in evidence.text
    assert "source_url" not in evidence.text


@pytest.mark.parametrize(
    "path",
    (
        f"/api/v1/research/runs/{RESEARCH_RUN_ID}?debug=1",
        "/api/v1/research/runs?limit=5&debug=1",
        "/api/v1/research/runs?limit=5&limit=10",
        f"/api/v1/research/runs/{RESEARCH_RUN_ID}/evidence?debug=1",
        f"/api/v1/research/runs/{RESEARCH_RUN_ID}/events?limit=10&debug=1",
        f"/api/v1/research/runs/{RESEARCH_RUN_ID}/events?limit=10&limit=20",
    ),
)
def test_research_queries_reject_unknown_or_duplicate_parameters(path: str) -> None:
    with client(
        research=ResearchFacade(),
        research_api=research_options(),
    ) as browser:
        response = browser.get(path)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"
