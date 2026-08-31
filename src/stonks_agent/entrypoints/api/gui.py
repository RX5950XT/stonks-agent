"""Loopback terminal: read-only market JSON plus one gated research command."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from stonks_agent.application.latest_market_data import (
    read_latest_market_data,
    read_market_quote,
    refresh_cached_quote,
)
from stonks_agent.application.market_freshness import MarketFreshnessPolicy
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.gui_model_settings import GuiModelSettingsView
from stonks_agent.domain.gui_paper import (
    GuiPaperIntegrityView,
    GuiPaperNavView,
    GuiPaperPortfolioView,
    GuiPaperRiskView,
    GuiPaperSafetyView,
)
from stonks_agent.domain.instrument_data import InstrumentDataQuery, InstrumentOverview
from stonks_agent.domain.latest_market_data import (
    MAX_LOOKBACK_DAYS,
    BarInterval,
    LatestMarketDataQuery,
    LatestMarketDataView,
    MarketQuoteView,
)
from stonks_agent.entrypoints.api.api_security import (
    ApiSecurityOptions,
    ApiSecurityPolicy,
    install_api_security,
)
from stonks_agent.entrypoints.api.envelope import (
    ApiError,
    ErrorEnvelope,
    SuccessEnvelope,
    error_envelope,
    success_envelope,
)
from stonks_agent.entrypoints.api.gui_model_settings import (
    GuiModelSettingsApiOptions,
    install_gui_model_settings_routes,
    model_settings_capability,
)
from stonks_agent.entrypoints.api.gui_research import (
    GuiResearchApiOptions,
    GuiResearchCapability,
    install_gui_research_routes,
    research_capability,
)
from stonks_agent.entrypoints.api.web_protection import (
    LOCAL_CONSOLE_CONTENT_SECURITY_POLICY,
)
from stonks_agent.ports.gui_model_settings import GuiModelSettingsPort
from stonks_agent.ports.gui_research import GuiResearchFacade
from stonks_agent.ports.instrument_data import InstrumentDataSource
from stonks_agent.ports.latest_market_data import LatestMarketDataSource

MAX_GUI_REQUEST_BYTES = 16_384
MAX_WATCHLIST_QUERY_CHARS = 4_096
QUOTE_CACHE_SECONDS = 20.0
PROVIDER_REQUESTS_PER_MINUTE = 30
PROVIDER_COOLDOWN_SECONDS = 15.0
PROVIDER_FAILURES_BEFORE_COOLDOWN = 3
_GUI_ROOT = Path(__file__).resolve().parents[2] / "gui"
_ASSETS: dict[str, tuple[str, str]] = {
    "product.css": ("assets/product.css", "text/css; charset=utf-8"),
    "product.js": ("assets/product.js", "text/javascript; charset=utf-8"),
    "market-data.js": ("assets/market-data.js", "text/javascript; charset=utf-8"),
    "research.js": ("assets/research.js", "text/javascript; charset=utf-8"),
    "settings.js": ("assets/settings.js", "text/javascript; charset=utf-8"),
    "terminal.css": ("assets/terminal.css", "text/css; charset=utf-8"),
    "terminal.js": ("assets/terminal.js", "text/javascript; charset=utf-8"),
    "favicon.svg": ("assets/favicon.svg", "image/svg+xml"),
}
_SHELL = "templates/index.html"
_MAX_ASSET_BYTES = 120_000
_ERROR_RESPONSES: dict[int | str, dict[str, type[ErrorEnvelope]]] = {
    status: {"model": ErrorEnvelope} for status in (400, 403, 404, 409, 429, 500, 503)
}


class ServiceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=32)
    detail: str = Field(min_length=1, max_length=128)
    state: str = Field(pattern=r"^(ready|absent|failed)$")


class PaperRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1, max_length=48)
    value: str = Field(min_length=1, max_length=64)


class PaperCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: str = Field(pattern=r"^(ready|unavailable)$")
    detail: str = Field(min_length=1, max_length=256)
    account_id: str | None = Field(default=None, max_length=128)
    rows: tuple[PaperRow, ...] = Field(default=(), max_length=32)
    portfolio: GuiPaperPortfolioView | None = None
    nav: GuiPaperNavView | None = None
    risk: GuiPaperRiskView | None = None
    integrity: GuiPaperIntegrityView | None = None
    safety: GuiPaperSafetyView | None = None


class GuiCapabilities(BaseModel):
    """What this console can honestly do right now, service by service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    services: tuple[ServiceStatus, ...] = Field(max_length=8)
    paper: PaperCapability
    research: GuiResearchCapability
    model_settings: GuiModelSettingsView


class WatchlistEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    quote: MarketQuoteView | None = None
    error: ApiError | None = None


class WatchlistView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quotes: tuple[WatchlistEntry, ...]


type PaperReader = Callable[[], PaperCapability]
type ServiceReader = Callable[[], Sequence[ServiceStatus]]


def create_gui_app(
    source: LatestMarketDataSource,
    *,
    clock: Callable[[], datetime] | None = None,
    api_security: ApiSecurityOptions | None = None,
    paper: PaperReader | None = None,
    research: GuiResearchFacade | None = None,
    model_settings: GuiModelSettingsPort | None = None,
    research_api: GuiResearchApiOptions | None = None,
    services: Sequence[ServiceStatus] | ServiceReader = (),
    market_freshness: MarketFreshnessPolicy | None = None,
    instrument_data: InstrumentDataSource | None = None,
) -> FastAPI:
    """Compose a local console without adding direct trading authority."""

    assets = {
        name: (_read_asset(_GUI_ROOT / path), media_type)
        for name, (path, media_type) in _ASSETS.items()
    }
    shell = _read_asset(_GUI_ROOT / _SHELL)
    selected_clock = clock or utc_now
    selected_research_api = research_api or GuiResearchApiOptions()
    app = FastAPI(
        title="Stonks Terminal",
        version="0.2.0",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    install_api_security(
        app,
        max_request_bytes=MAX_GUI_REQUEST_BYTES,
        options=_console_security(api_security),
    )
    app.add_middleware(_LoopbackOnlyMiddleware)
    market = _MarketService(
        source,
        selected_clock,
        freshness=market_freshness,
    )
    instrument = _InstrumentService(instrument_data, selected_clock)
    app.add_api_route(
        "/",
        _StaticEndpoint(shell, "text/html; charset=utf-8", store=False),
        methods=["GET"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    for name, (content, media_type) in assets.items():
        app.add_api_route(
            f"/assets/{name}",
            _StaticEndpoint(content, media_type, store=True),
            methods=["GET"],
            response_class=Response,
            include_in_schema=False,
        )
    app.add_api_route(
        "/api/v1/capabilities",
        _CapabilitiesEndpoint(
            services if callable(services) else tuple(services),
            paper,
            research_capability(research, selected_research_api),
            model_settings,
        ),
        methods=["GET"],
        response_model=SuccessEnvelope[GuiCapabilities],
        responses=_ERROR_RESPONSES,
    )
    app.add_api_route(
        "/api/v1/market/bars",
        _BarsEndpoint(market),
        methods=["GET"],
        response_model=SuccessEnvelope[LatestMarketDataView],
        responses=_ERROR_RESPONSES,
        openapi_extra={"parameters": _BARS_PARAMETERS},
    )
    app.add_api_route(
        "/api/v1/market/quotes",
        _WatchlistEndpoint(market),
        methods=["GET"],
        response_model=SuccessEnvelope[WatchlistView],
        responses=_ERROR_RESPONSES,
        openapi_extra={"parameters": _WATCHLIST_PARAMETERS},
    )
    app.add_api_route(
        "/api/v1/market-data/latest",
        _BarsEndpoint(market),
        methods=["GET"],
        response_model=SuccessEnvelope[LatestMarketDataView],
        responses=_ERROR_RESPONSES,
        openapi_extra={"parameters": _BARS_PARAMETERS},
    )
    app.add_api_route(
        "/api/v1/instrument/overview",
        _InstrumentEndpoint(instrument),
        methods=["GET"],
        response_model=SuccessEnvelope[InstrumentOverview],
        responses=_ERROR_RESPONSES,
        openapi_extra={"parameters": _INSTRUMENT_PARAMETERS},
    )
    install_gui_research_routes(
        app,
        research,
        options=selected_research_api,
        clock=selected_clock,
        model_ready=(
            (lambda: _model_settings_ready(model_settings))
            if model_settings is not None
            else None
        ),
    )
    install_gui_model_settings_routes(
        app,
        model_settings,
        options=GuiModelSettingsApiOptions(
            intent_token=selected_research_api.intent_token,
        ),
    )
    return app


def _model_settings_ready(settings: GuiModelSettingsPort) -> bool:
    try:
        view = settings.view()
    except Exception:
        return False
    return view.state == "configured" and view.api_key_configured and view.verified


def _console_security(options: ApiSecurityOptions | None) -> ApiSecurityOptions:
    """Grant same-origin scripts exactly one policy-checked exception."""

    base = options or ApiSecurityOptions()
    if (
        base.policy.content_security_policy
        != ApiSecurityPolicy().content_security_policy
    ):
        return base
    return ApiSecurityOptions(
        policy=base.policy.model_copy(
            update={
                "content_security_policy": LOCAL_CONSOLE_CONTENT_SECURITY_POLICY,
            }
        ),
        rate_limit_store=base.rate_limit_store,
        clock=base.clock,
        cookie_auth=base.cookie_auth,
    )


class _MarketService:
    """One bounded read path with a short cache and no cross-symbol coupling."""

    def __init__(
        self,
        source: LatestMarketDataSource,
        clock: Callable[[], datetime],
        *,
        freshness: MarketFreshnessPolicy | None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._source = source
        self._clock = clock
        self._freshness = freshness
        self._monotonic = monotonic_clock
        self._lock = Lock()
        self._cache: dict[tuple[str, str], tuple[float, MarketQuoteView]] = {}
        self._gate = _ProviderRequestGate(monotonic_clock)

    def bars(self, query: LatestMarketDataQuery) -> Result[LatestMarketDataView]:
        admitted = self._gate.admit()
        if isinstance(admitted, Failure):
            return admitted
        result = read_latest_market_data(
            query,
            source=self._source,
            clock=self._clock,
            freshness=self._freshness,
        )
        self._gate.record(result)
        return result

    def quote(self, query: LatestMarketDataQuery) -> Result[MarketQuoteView]:
        key = (query.symbol, query.interval.value)
        cached = self._cached(key)
        if cached is not None:
            return Success(cached)
        admitted = self._gate.admit()
        if isinstance(admitted, Failure):
            return admitted
        result = read_market_quote(
            query,
            source=self._source,
            clock=self._clock,
            freshness=self._freshness,
        )
        self._gate.record(result)
        if isinstance(result, Success):
            with self._lock:
                if len(self._cache) > 256:
                    self._cache.clear()
                self._cache[key] = (self._monotonic(), result.value)
        return result

    def watchlist(self, symbols: Sequence[str]) -> WatchlistView:
        if not symbols:
            return WatchlistView(quotes=())
        workers = min(4, len(symbols))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = tuple(pool.map(self._entry, symbols))
        return WatchlistView(quotes=results)

    def _entry(self, symbol: str) -> WatchlistEntry:
        query = _query(symbol, lookback_days=7, interval=BarInterval.MINUTE)
        if isinstance(query, Failure):
            return WatchlistEntry(symbol=symbol, error=_api_error(query))
        result = self.quote(query)
        if isinstance(result, Failure):
            return WatchlistEntry(symbol=symbol, error=_api_error(result))
        return WatchlistEntry(symbol=symbol, quote=result.value)

    def _cached(self, key: tuple[str, str]) -> MarketQuoteView | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            stored_at, view = entry
            if self._monotonic() - stored_at > QUOTE_CACHE_SECONDS:
                del self._cache[key]
                return None
            try:
                return refresh_cached_quote(
                    view,
                    served_at=self._clock(),
                    freshness=self._freshness,
                )
            except Exception:
                del self._cache[key]
                return None


class _InstrumentService:
    """One bounded company-data read with a short process-local cache."""

    def __init__(
        self,
        source: InstrumentDataSource | None,
        clock: Callable[[], datetime],
        *,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._source = source
        self._clock = clock
        self._monotonic = monotonic_clock
        self._lock = Lock()
        self._cache: dict[str, tuple[float, InstrumentOverview]] = {}
        self._gate = _ProviderRequestGate(monotonic_clock)

    def overview(self, symbol: str) -> Result[InstrumentOverview]:
        symbol = symbol.strip().upper()
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            return _failure(
                ErrorCode.CONFIGURATION_INVALID, "Instrument data clock is invalid"
            )
        normalized = now.astimezone(UTC)
        with self._lock:
            cached = self._cache.get(symbol)
            if cached is not None and self._monotonic() - cached[0] <= 60.0:
                return Success(cached[1])
        if self._source is None:
            return _failure(
                ErrorCode.DATA_UNAVAILABLE, "Instrument data is not composed"
            )
        admitted = self._gate.admit()
        if isinstance(admitted, Failure):
            return admitted
        try:
            query = InstrumentDataQuery(symbol=symbol, as_of=normalized)
            result = self._source.fetch(query, observed_at=normalized)
        except (TypeError, ValueError):
            result = _failure(
                ErrorCode.INVALID_INPUT, "Instrument data query is invalid"
            )
        self._gate.record(result)
        if isinstance(result, Success):
            with self._lock:
                self._cache[symbol] = (self._monotonic(), result.value)
        return result


class _InstrumentEndpoint:
    def __init__(self, instrument: _InstrumentService) -> None:
        self._instrument = instrument

    def __call__(self, request: Request) -> JSONResponse:
        parameters = request.query_params
        if any(name != "symbol" for name in parameters):
            return _error_response(_invalid_query())
        values = parameters.getlist("symbol")
        if len(values) != 1:
            return _error_response(_invalid_query())
        result = self._instrument.overview(values[0])
        if isinstance(result, Failure):
            return _error_response(result)
        return _success(result.value)


class _ProviderRequestGate:
    """Bound fan-out and pause briefly after repeated provider failures."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = Lock()
        self._requests: deque[float] = deque()
        self._consecutive_failures = 0
        self._blocked_until = 0.0

    def admit(self) -> Success[bool] | Failure:
        now = self._clock()
        with self._lock:
            self._discard_expired(now)
            if (
                now < self._blocked_until
                or len(self._requests) >= PROVIDER_REQUESTS_PER_MINUTE
            ):
                return _rate_limited()
            self._requests.append(now)
        return Success(True)

    def record(self, result: Result[object]) -> None:
        with self._lock:
            if isinstance(result, Success):
                self._consecutive_failures = 0
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= PROVIDER_FAILURES_BEFORE_COOLDOWN:
                self._blocked_until = self._clock() + PROVIDER_COOLDOWN_SECONDS
                self._consecutive_failures = 0

    def _discard_expired(self, now: float) -> None:
        while self._requests and now - self._requests[0] >= 60.0:
            self._requests.popleft()


def _rate_limited() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.RATE_LIMITED,
            message="Market-data provider request budget is exhausted",
        )
    )


class _StaticEndpoint:
    def __init__(self, content: str, media_type: str, *, store: bool) -> None:
        self._content = content
        self._media_type = media_type
        self._cache_control = "no-store" if not store else "public, max-age=300"

    def __call__(self) -> Response:
        return Response(
            content=self._content,
            media_type=self._media_type,
            headers={"Cache-Control": self._cache_control},
        )


class _CapabilitiesEndpoint:
    def __init__(
        self,
        services: tuple[ServiceStatus, ...] | ServiceReader,
        paper: PaperReader | None,
        research: GuiResearchCapability,
        model_settings: GuiModelSettingsPort | None,
    ) -> None:
        self._services = services
        self._paper = paper
        self._research = research
        self._model_settings = model_settings

    def __call__(self, request: Request) -> JSONResponse:
        if tuple(request.query_params):
            return _error_response(_invalid_query())
        return _success(
            GuiCapabilities(
                services=self._service_state(),
                paper=self._paper_state(),
                research=self._research,
                model_settings=model_settings_capability(self._model_settings),
            )
        )

    def _service_state(self) -> tuple[ServiceStatus, ...]:
        if not callable(self._services):
            return self._services
        try:
            values = tuple(self._services())
            if len(values) > 8 or not all(
                isinstance(item, ServiceStatus) for item in values
            ):
                raise ValueError("service health projection is invalid")
            return values
        except Exception:
            return (
                ServiceStatus(
                    name="runtime",
                    detail="即時 health probe 失敗",
                    state="failed",
                ),
            )

    def _paper_state(self) -> PaperCapability:
        if self._paper is None:
            return PaperCapability(
                state="unavailable",
                detail=(
                    "此工作階段沒有組合 paper 投資組合服務。"
                    "請以 --with-paper 啟動並提供 PostgreSQL。"
                ),
            )
        try:
            return self._paper()
        except Exception:
            return PaperCapability(
                state="unavailable",
                detail="Paper 投資組合讀取失敗。不顯示可能過期的數字。",
            )


class _BarsEndpoint:
    def __init__(self, market: _MarketService) -> None:
        self._market = market

    def __call__(self, request: Request) -> JSONResponse:
        query = _query_from_request(request)
        if isinstance(query, Failure):
            return _error_response(query)
        result = self._market.bars(query)
        if isinstance(result, Failure):
            return _error_response(result)
        return _success(result.value)


class _WatchlistEndpoint:
    def __init__(self, market: _MarketService) -> None:
        self._market = market

    def __call__(self, request: Request) -> JSONResponse:
        symbols = _symbols_from_request(request)
        if isinstance(symbols, Failure):
            return _error_response(symbols)
        return _success(self._market.watchlist(symbols.value))


class _LoopbackOnlyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] == "websocket":
            await send(
                {
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "WebSocket is unsupported",
                }
            )
            return
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        denial = None
        if not _is_loopback_client(scope) or not _is_loopback_host(scope):
            denial = "GUI is available only on loopback"
        elif not _is_allowed_api_origin(scope):
            denial = "GUI API request origin is not allowed"
        if denial is not None:
            envelope = error_envelope(
                StructuredError(
                    code=ErrorCode.FORBIDDEN,
                    message=denial,
                )
            )
            response = JSONResponse(
                status_code=envelope.status,
                content=envelope.model_dump(mode="json"),
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _query(
    symbol: str,
    *,
    lookback_days: int,
    interval: BarInterval,
) -> LatestMarketDataQuery | Failure:
    try:
        return LatestMarketDataQuery(
            symbol=symbol,
            lookback_days=lookback_days,
            interval=interval,
        )
    except (TypeError, ValueError):
        return _invalid_query()


def _query_from_request(request: Request) -> LatestMarketDataQuery | Failure:
    parameters = request.query_params
    if any(name not in {"symbol", "lookback_days", "interval"} for name in parameters):
        return _invalid_query()
    symbols = parameters.getlist("symbol")
    lookbacks = parameters.getlist("lookback_days")
    intervals = parameters.getlist("interval")
    if len(symbols) != 1 or len(lookbacks) > 1 or len(intervals) > 1:
        return _invalid_query()
    try:
        lookback_days = int(lookbacks[0]) if lookbacks else 30
        interval = BarInterval(intervals[0]) if intervals else BarInterval.DAY
    except ValueError:
        return _invalid_query()
    return _query(symbols[0], lookback_days=lookback_days, interval=interval)


def _symbols_from_request(request: Request) -> Success[tuple[str, ...]] | Failure:
    parameters = request.query_params
    if any(name != "symbols" for name in parameters):
        return _invalid_query()
    values = parameters.getlist("symbols")
    if len(values) != 1 or len(values[0]) > MAX_WATCHLIST_QUERY_CHARS:
        return _invalid_query()
    symbols = tuple(
        item.strip().upper() for item in values[0].split(",") if item.strip()
    )
    if not symbols:
        return _invalid_query()
    if len(set(symbols)) != len(symbols):
        return _invalid_query()
    return Success(symbols)


def _invalid_query() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.INVALID_INPUT,
            message="Market-data query is invalid",
        )
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))


def _success(value: BaseModel) -> JSONResponse:
    return JSONResponse(
        content=success_envelope(value).model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


def _api_error(result: Failure) -> ApiError:
    """Reuse the envelope's public error shape so one symbol cannot leak."""

    return error_envelope(result.error).error


def _error_response(result: Failure) -> JSONResponse:
    envelope = error_envelope(result.error)
    return JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


def _is_loopback_client(scope: Scope) -> bool:
    client = scope.get("client")
    if not client or not isinstance(client[0], str):
        return False
    try:
        return ip_address(client[0]).is_loopback
    except ValueError:
        return False


def _is_loopback_host(scope: Scope) -> bool:
    values = [
        value for name, value in scope.get("headers", []) if name.lower() == b"host"
    ]
    if len(values) != 1:
        return False
    raw_value = values[0]
    if not isinstance(raw_value, bytes):
        return False
    try:
        raw = raw_value.decode("ascii")
        parsed = urlsplit(f"//{raw}")
        port = parsed.port
    except (UnicodeDecodeError, ValueError):
        return False
    hostname = parsed.hostname
    if (
        not isinstance(hostname, str)
        or hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    ):
        return False
    rendered_host = "[::1]" if hostname == "::1" else hostname
    rendered_port = "" if port is None else f":{port}"
    return raw == f"{rendered_host}{rendered_port}"


def _is_allowed_api_origin(scope: Scope) -> bool:
    path = scope.get("path")
    if not isinstance(path, str) or not path.startswith("/api/"):
        return True
    request = Request(scope)
    origins = request.headers.getlist("origin")
    hosts = request.headers.getlist("host")
    expected = f"{request.url.scheme}://{hosts[0]}" if len(hosts) == 1 else None
    if origins and (len(origins) != 1 or origins[0] != expected):
        return False
    fetch_sites = request.headers.getlist("sec-fetch-site")
    return not fetch_sites or fetch_sites == ["same-origin"]


def _read_asset(path: Path) -> str:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > _MAX_ASSET_BYTES
    ):
        raise RuntimeError("GUI asset is invalid")
    return path.read_text(encoding="utf-8")


_BARS_PARAMETERS = [
    {
        "name": "symbol",
        "in": "query",
        "required": True,
        "schema": {
            "type": "string",
            "minLength": 1,
            "maxLength": 32,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9.\-]*$",
        },
    },
    {
        "name": "interval",
        "in": "query",
        "required": False,
        "schema": {
            "type": "string",
            "enum": [item.value for item in BarInterval],
            "default": BarInterval.DAY.value,
        },
    },
    {
        "name": "lookback_days",
        "in": "query",
        "required": False,
        "schema": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LOOKBACK_DAYS,
            "default": 30,
        },
    },
]
_WATCHLIST_PARAMETERS = [
    {
        "name": "symbols",
        "in": "query",
        "required": True,
        "schema": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_WATCHLIST_QUERY_CHARS,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9.,\-]*$",
        },
    }
]
_INSTRUMENT_PARAMETERS = [
    {
        "name": "symbol",
        "in": "query",
        "required": True,
        "schema": {
            "type": "string",
            "minLength": 1,
            "maxLength": 32,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9.\-]*$",
        },
    }
]
