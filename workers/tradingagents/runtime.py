"""Pinned TradingAgents bridge with request-scoped canonical evidence tools."""

from __future__ import annotations

import copy
from collections.abc import Callable
from time import monotonic
from typing import Any

from stonks_contracts.common import ModelUsage
from workers.tradingagents.adapter import (
    EvidenceCategory,
    RuntimeAnalysis,
    RuntimeTelemetry,
    TradingAgentsRequest,
)

MODEL_PROXY_ORIGIN = "http://model-proxy:8000/v1"
MODEL_PROXY_MODEL = "stonks-research"


class PinnedTradingAgentsRuntime:
    """Run the pinned graph after replacing every upstream data tool."""

    __slots__ = ("_selected_analysts",)

    def __init__(self, *, selected_analysts: tuple[str, ...]) -> None:
        self._selected_analysts = selected_analysts

    def run(self, request: TradingAgentsRequest) -> RuntimeAnalysis:
        graph_module, graph_class, default_config = _load_upstream()
        _install_canonical_facade(graph_module, request)
        callback = _TelemetryCallback()
        config = _runtime_config(default_config)
        graph = graph_class(
            selected_analysts=self._selected_analysts,
            debug=False,
            config=config,
            callbacks=[callback],
        )
        graph._resolve_pending_entries = lambda _: None
        final_state, decision = graph.propagate(
            request.symbol,
            request.as_of.date().isoformat(),
            asset_type="stock",
        )
        recommendation = _normalize_recommendation(decision)
        thesis, warnings = _extract_thesis(final_state)
        return RuntimeAnalysis(
            recommendation=recommendation,
            thesis=thesis,
            telemetry=RuntimeTelemetry(
                model_usage=callback.model_usage,
                tool_latency_ms=callback.tool_latency_ms,
                warnings=warnings,
                source_refs=request.allowed_evidence_ids,
            ),
        )


def _load_upstream() -> tuple[Any, type[Any], dict[str, Any]]:
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph import trading_graph
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return trading_graph, TradingAgentsGraph, DEFAULT_CONFIG


def _runtime_config(default: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(default)
    config.update(
        {
            "llm_provider": "openai_compatible",
            "deep_think_llm": MODEL_PROXY_MODEL,
            "quick_think_llm": MODEL_PROXY_MODEL,
            "backend_url": MODEL_PROXY_ORIGIN,
            "llm_max_retries": 0,
            "checkpoint_enabled": False,
            "results_dir": "/tmp/results",
            "data_cache_dir": "/tmp/cache",
            "memory_log_path": "/tmp/memory/trading_memory.md",
            "data_vendors": {
                "core_stock_apis": "canonical_facade",
                "technical_indicators": "canonical_facade",
                "fundamental_data": "canonical_facade",
                "news_data": "canonical_facade",
                "macro_data": "canonical_facade",
                "prediction_markets": "canonical_facade",
            },
            "tool_vendors": {},
        }
    )
    return config


def _install_canonical_facade(module: Any, request: TradingAgentsRequest) -> None:
    from langchain_core.tools import tool

    content = _content_by_category(request)

    @tool
    def stock_data(symbol: str, start_date: str, end_date: str) -> str:
        """Return request-scoped canonical market evidence."""
        del start_date, end_date
        _require_symbol(symbol, request.symbol)
        return content[EvidenceCategory.MARKET]

    @tool
    def indicators(
        symbol: str,
        indicator: str,
        curr_date: str,
        look_back_days: int = 30,
    ) -> str:
        """Return request-scoped canonical indicator evidence."""
        del indicator, curr_date, look_back_days
        _require_symbol(symbol, request.symbol)
        return content[EvidenceCategory.MARKET]

    @tool
    def market_snapshot(
        symbol: str,
        curr_date: str,
        look_back_days: int = 30,
    ) -> str:
        """Return request-scoped verified canonical market evidence."""
        del curr_date, look_back_days
        _require_symbol(symbol, request.symbol)
        return content[EvidenceCategory.MARKET]

    @tool
    def fundamentals(ticker: str, curr_date: str | None = None) -> str:
        """Return request-scoped canonical fundamental evidence."""
        del curr_date
        _require_symbol(ticker, request.symbol)
        return content[EvidenceCategory.FUNDAMENTALS]

    @tool
    def news(ticker: str, curr_date: str | None = None) -> str:
        """Return request-scoped canonical news evidence."""
        del curr_date
        _require_symbol(ticker, request.symbol)
        return content[EvidenceCategory.NEWS]

    @tool
    def global_news(curr_date: str | None = None) -> str:
        """Return request-scoped canonical macro evidence."""
        del curr_date
        return content[EvidenceCategory.MACRO]

    @tool
    def social(ticker: str, curr_date: str | None = None) -> str:
        """Return request-scoped canonical sentiment evidence."""
        del curr_date
        _require_symbol(ticker, request.symbol)
        return content[EvidenceCategory.SENTIMENT]

    replacements = {
        "get_stock_data": stock_data,
        "get_indicators": indicators,
        "get_verified_market_snapshot": market_snapshot,
        "get_fundamentals": fundamentals,
        "get_balance_sheet": fundamentals,
        "get_cashflow": fundamentals,
        "get_income_statement": fundamentals,
        "get_news": news,
        "get_global_news": global_news,
        "get_insider_transactions": social,
        "get_macro_indicators": global_news,
        "get_prediction_markets": social,
    }
    for name, replacement in replacements.items():
        setattr(module, name, replacement)
    module.resolve_instrument_identity = lambda ticker: _identity(ticker, request)


def _content_by_category(request: TradingAgentsRequest) -> dict[EvidenceCategory, str]:
    grouped: dict[EvidenceCategory, list[str]] = {
        category: [] for category in EvidenceCategory
    }
    for item in request.evidence:
        grouped[item.category].append(
            f"UNTRUSTED EVIDENCE {item.evidence_id} ({item.artifact_ref})\n{item.content}"
        )
    return {
        category: "\n\n".join(values) if values else "No scoped evidence available."
        for category, values in grouped.items()
    }


def _identity(ticker: str, request: TradingAgentsRequest) -> dict[str, str]:
    _require_symbol(ticker, request.symbol)
    return {"symbol": request.symbol, "name": request.symbol}


def _require_symbol(actual: str, expected: str) -> None:
    if actual.upper() != expected:
        raise ValueError("tool symbol exceeded request scope")


def _normalize_recommendation(value: object) -> str:
    normalized = str(value).strip().lower()
    mapping = {
        "buy": "Buy",
        "overweight": "Overweight",
        "hold": "Hold",
        "underweight": "Underweight",
        "sell": "Sell",
    }
    if normalized not in mapping:
        raise ValueError("upstream recommendation is invalid")
    return mapping[normalized]


def _extract_thesis(state: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(state, dict):
        raise ValueError("upstream state is invalid")
    value = state.get("final_trade_decision")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("upstream thesis is invalid")
    encoded = value.strip()
    if len(encoded) <= 16_384:
        return encoded, ()
    return encoded[:16_384], ("upstream_thesis_truncated",)


class _TelemetryCallback:
    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._llm_started: list[float] = []
        self._tool_started: list[float] = []
        self._model_usage: list[ModelUsage] = []
        self._tool_latency: list[int] = []

    @property
    def model_usage(self) -> tuple[ModelUsage, ...]:
        return tuple(self._model_usage)

    @property
    def tool_latency_ms(self) -> tuple[int, ...]:
        return tuple(self._tool_latency)

    def on_llm_start(self, *_: object, **__: object) -> None:
        self._llm_started.append(self._clock())

    def on_llm_end(self, response: object, **_: object) -> None:
        started = self._llm_started.pop() if self._llm_started else self._clock()
        usage = _extract_usage(response)
        self._model_usage.append(
            ModelUsage(
                input_tokens=usage[0],
                output_tokens=usage[1],
                latency_ms=max(0, int((self._clock() - started) * 1000)),
            )
        )

    def on_tool_start(self, *_: object, **__: object) -> None:
        self._tool_started.append(self._clock())

    def on_tool_end(self, *_: object, **__: object) -> None:
        started = self._tool_started.pop() if self._tool_started else self._clock()
        self._tool_latency.append(max(0, int((self._clock() - started) * 1000)))


def _extract_usage(response: object) -> tuple[int, int]:
    llm_output = getattr(response, "llm_output", None)
    if not isinstance(llm_output, dict):
        return 0, 0
    usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
    if not isinstance(usage, dict):
        return 0, 0
    return (
        _safe_int(usage.get("prompt_tokens", usage.get("input_tokens", 0))),
        _safe_int(usage.get("completion_tokens", usage.get("output_tokens", 0))),
    )


def _safe_int(value: object) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )
