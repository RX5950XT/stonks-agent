from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "src" / "stonks_agent" / "gui"

# The console now ships local scripts, so "no script tag" is no longer the
# control. These are: nothing may load from another origin, nothing may execute
# from a string, and nothing may turn provider data into markup.
_FORBIDDEN_SUBSTRINGS = (
    "http://",
    "https://",
    "//cdn",
    "javascript:",
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "localStorage",
    "sessionStorage",
    "indexedDB",
    "XMLHttpRequest",
    "WebSocket",
    "importScripts",
    "@import",
)
_INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
_SVG_NAMESPACE = 'xmlns="http://www.w3.org/2000/svg"'


def test_gui_assets_are_bounded_local_and_free_of_remote_or_string_code() -> None:
    files = tuple(sorted(path for path in ASSETS.rglob("*") if path.is_file()))

    assert {path.relative_to(ASSETS).as_posix() for path in files} == {
        "assets/favicon.svg",
        "assets/market-data.js",
        "assets/product.css",
        "assets/product.js",
        "assets/research.js",
        "assets/settings.js",
        "assets/terminal.css",
        "assets/terminal.js",
        "templates/index.html",
    }
    assert sum(path.stat().st_size for path in files) <= 150_000
    for path in files:
        assert not path.is_symlink()
        # The SVG namespace is an identifier, not a fetch; strip that exact
        # literal so the remote-origin ban still means something everywhere else.
        content = path.read_text(encoding="utf-8").replace(_SVG_NAMESPACE, "")
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in content, f"{path.name} contains {forbidden}"


def test_markup_carries_no_inline_script_or_event_handlers() -> None:
    template = (ASSETS / "templates" / "index.html").read_text(encoding="utf-8")

    assert _INLINE_HANDLER.search(template) is None
    assert template.count("<script") == 5
    assert '<script src="/assets/market-data.js" defer></script>' in template
    assert '<script src="/assets/settings.js" defer></script>' in template
    assert '<script src="/assets/terminal.js" defer></script>' in template
    assert '<script src="/assets/research.js" defer></script>' in template
    assert '<script src="/assets/product.js" defer></script>' in template
    assert "<script>" not in template


def test_console_script_stays_strict_and_self_contained() -> None:
    scripts = tuple(sorted((ASSETS / "assets").glob("*.js")))
    script = "\n".join(path.read_text(encoding="utf-8") for path in scripts)

    assert '"use strict";' in script
    assert "import " not in script
    assert "fetch(" in script
    assert "EventSource" in script
    assert "RESEARCH <代號>" in script
    assert "textContent" in script
    assert all(path.read_text(encoding="utf-8").count("\n") < 800 for path in scripts)


def test_gui_exposes_the_backend_driven_primary_journey_with_research_chat() -> None:
    template = (ASSETS / "templates" / "index.html").read_text(encoding="utf-8")
    terminal = (ASSETS / "assets" / "terminal.js").read_text(encoding="utf-8")
    research = (ASSETS / "assets" / "research.js").read_text(encoding="utf-8")
    product = (ASSETS / "assets" / "product.js").read_text(encoding="utf-8")

    assert '<form class="market-search" id="market-search"' in template
    assert 'id="symbol-search"' in template
    assert 'id="research-action"' in template
    assert 'id="model-settings-form"' in template
    assert 'id="model-api-key"' in template
    assert 'type="password"' in template
    assert 'autocomplete="off"' in template
    assert 'id="model-settings-save"' in template
    assert 'id="model-settings-clear"' in template
    assert 'aria-describedby="research-action-note"' in template
    assert 'id="research-progress"' in template
    assert 'id="research-results"' in template
    assert 'id="research-report"' in template
    assert 'id="research-history"' in template
    assert 'id="research-evidence"' in template
    assert 'id="market-table-body"' in template
    assert 'id="panel-overview"' in template
    assert 'id="instrument-dashboard"' in template
    assert 'id="panel-provenance"' not in template
    assert "資料檢查" not in template
    assert 'id="research-chat"' in template
    assert template.index('id="research-chat"') < template.index('id="model-settings"')
    assert 'addEventListener("submit"' in terminal
    assert 'addEventListener("click"' in terminal
    assert '"stonks:research"' in terminal
    assert "research.queued" in research
    assert "research.succeeded" in research
    assert "evidence_ids" in research
    assert "kronos_forecast" in research
    assert "kronos_alpha" in research
    assert "paper_decision" in research
    assert "/api/v1/research/runs?limit=10" in research
    assert "/evidence" in research
    assert ".portfolio" in product
    assert ".risk" in product
    assert ".safety" in product
    assert ".cash_value" in product
    assert ".position_value" in product
    assert ".cumulative_fees" in product
    assert ".realized_pnl" in product
    assert ".sellable" in product
    assert ".reserved" in product
    assert ".decided_at" in product
    assert ".ledger_hash" in product
    assert "dashboard-columns" in product
    assert "公司與財報" in product
    assert "近期申報" in product
    assert "forecast.forecast_id" in research
    assert "forecast.generated_at" in research
    assert "forecast.horizon_bars" in research
    assert "forecast.median_return" in research
    assert "forecast.warnings" in research
    assert "item.as_of" in research
    assert "item.confidence" in research
    assert "item.error_code" in research
    assert "model.api_key_configured" in terminal
    assert "model.verified" in terminal
    assert 'interval: "1m"' in terminal
    assert "AUTO_REFRESH_MS" in terminal
    assert "document.visibilityState" in terminal
    assert "loadCapabilities();\n    if (state.symbol)" in terminal
    assert 'next.searchParams.set("s", state.symbol)' in terminal
    assert 'window.addEventListener("hashchange"' not in terminal
    assert "if (!quiet) dom.symbolSearch.value = target;" in terminal
    assert "dom.symbolSearch.value = state.symbol;" not in terminal
    assert "if (quiet) {" in terminal
    assert "保留最後成功資料" in terminal
    assert "view.freshness" in terminal
    assert ".quality" in product
    assert 'view.is_real_time ? "即時" : "非逐筆即時"' in terminal
    assert "staleAfter" not in terminal
    assert "kronos_summary" not in research


def test_model_settings_keep_safe_defaults_out_of_the_user_form() -> None:
    template = (ASSETS / "templates" / "index.html").read_text(encoding="utf-8")
    settings = (ASSETS / "assets" / "settings.js").read_text(encoding="utf-8")

    assert 'id="model-base-url"' in template
    assert 'id="model-id"' in template
    assert 'id="model-api-key"' in template
    assert 'id="model-provider"' not in template
    assert 'id="model-endpoint"' not in template
    assert 'id="model-max-output"' not in template
    assert 'id="model-input-cost"' not in template
    assert "max_output_tokens" not in settings
    assert "input_cost_per_million" not in settings


def test_gui_has_safe_chinese_command_translation_and_chart_pan() -> None:
    template = (ASSETS / "templates" / "index.html").read_text(encoding="utf-8")
    terminal = (ASSETS / "assets" / "terminal.js").read_text(encoding="utf-8")
    market_data = (ASSETS / "assets" / "market-data.js").read_text(encoding="utf-8")
    style = (ASSETS / "assets" / "terminal.css").read_text(encoding="utf-8")

    assert "中文操作" in template
    assert "也支援進階命令" in template
    assert "不會執行任意系統指令" in template
    assert "自然語言" in terminal
    assert "chartStart" in terminal
    assert "pointerdown" in terminal
    assert "onChartWheel" in terminal
    assert 'id="ranges"' in template
    assert "rangeOf" in terminal
    assert "lookback_days: rangeOf(state.range, state.interval).days" in terminal
    assert 'next.searchParams.set("r", state.range)' in terminal
    assert 'interval("1Y", "年線"' in market_data
    assert 'range("ytd"' in market_data
    assert 'range("5y"' in market_data
    assert 'range("10y"' in market_data
    assert 'range("max"' in market_data
    assert "event.isPrimary" in terminal
    assert "event.button !== 0" in terminal
    assert "touch-action: pan-y" in style


def test_quiet_refresh_failure_releases_busy_state_without_hiding_last_quote() -> None:
    terminal = (ASSETS / "assets" / "terminal.js").read_text(encoding="utf-8")
    failure_branch = terminal.split("if (!result.ok) {", maxsplit=1)[1].split(
        "renderQuote(result.data);", maxsplit=1
    )[0]
    loading_helper = terminal.split("function finishLoading() {", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]

    assert "if (!quiet) dom.quoteInterval.textContent" in terminal
    assert "finishLoading();" in failure_branch
    assert "state.loading = false;" in loading_helper
    assert 'dom.quotePanel.setAttribute("aria-busy", "false");' in loading_helper
    assert 'dom.chartPanel.setAttribute("aria-busy", "false");' in loading_helper
    assert "保留最後成功資料" in failure_branch


def test_every_script_dom_target_exists_once_and_tokens_keep_three_layers() -> None:
    template = (ASSETS / "templates" / "index.html").read_text(encoding="utf-8")
    scripts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ASSETS / "assets").glob("*.js"))
    )
    style = (ASSETS / "assets" / "terminal.css").read_text(encoding="utf-8")
    template_ids = re.findall(r'\bid="([^"]+)"', template)
    script_targets = set(re.findall(r'\bel\("([^"]+)"\)', scripts))

    assert len(template_ids) == len(set(template_ids))
    assert script_targets <= set(template_ids)
    assert "/* Primitives */" in style
    assert "/* Semantic */" in style
    assert "/* Components */" in style


def test_gui_dark_shell_keeps_runtime_readiness_without_duplicate_status_strip() -> (
    None
):
    template = (ASSETS / "templates" / "index.html").read_text(encoding="utf-8")
    style = (ASSETS / "assets" / "terminal.css").read_text(encoding="utf-8")
    product = (ASSETS / "assets" / "product.js").read_text(encoding="utf-8")

    assert '<meta name="color-scheme" content="dark">' in template
    assert '<meta name="theme-color" content="#0d1117">' in template
    assert "color-scheme: dark" in style
    assert "color-scheme: light" not in style
    assert template.count('id="runtime-readiness"') == 1
    assert '"runtime-readiness"' in product
    assert 'class="status-strip"' not in template
    assert "status-strip" not in style
    assert "capability-market-state" not in template + product
    assert "capability-research-state" not in template + product
    assert "capability-model-state" not in template + product
    assert "capability-data-state" not in template + product
    assert "capability-map" not in template
    assert "Backend capability map" not in template


def test_composite_controls_render_one_focus_ring() -> None:
    style = (ASSETS / "assets" / "terminal.css").read_text(encoding="utf-8")
    global_focus = style.split(":focus-visible {", 1)[1].split("}", 1)[0]
    search_focus = style.split(".search-control:focus-within {", 1)[1].split("}", 1)[0]

    assert "outline: 2px solid var(--color-primary)" in global_focus
    assert "box-shadow" not in global_focus
    assert "box-shadow: var(--focus-ring)" in search_focus


def test_gui_secret_fields_avoid_password_manager_persistence_semantics() -> None:
    template = (ASSETS / "templates" / "index.html").read_text(encoding="utf-8")
    settings = (ASSETS / "assets" / "settings.js").read_text(encoding="utf-8")

    assert 'id="model-id"' in template
    assert 'id="model-api-key"' in template
    assert 'autocomplete="username"' not in template
    assert 'autocomplete="new-password"' not in template
    assert template.count('autocomplete="off"') >= 3
    assert 'window.addEventListener("pagehide", () =>' in settings
    pagehide = settings.split('window.addEventListener("pagehide", () => {', 1)[
        1
    ].split("});", 1)[0]
    assert 'dom.key.value = "";' in pagehide
    assert "hideKey();" in pagehide


def test_capability_map_requires_verified_model_and_live_research_service() -> None:
    product = (ASSETS / "assets" / "product.js").read_text(encoding="utf-8")

    assert '"research"' in product
    assert "api_key" in product
    assert "verified" in product
    assert 'configured:"已設定"' in product
    assert 'configured:"已驗證"' not in product


def test_market_labels_follow_the_canonical_symbol_suffixes() -> None:
    terminal = (ASSETS / "assets" / "terminal.js").read_text(encoding="utf-8")

    assert 'upper.endsWith(".TW") || upper.endsWith(".TWO")' in terminal
    assert 'upper.endsWith(".HK")' in terminal
    assert 'return "台灣股票";' in terminal
    assert 'return "香港股票";' in terminal


def test_gui_removes_verified_dead_refresh_chain_and_legacy_mobile_floor() -> None:
    style = (ASSETS / "assets" / "terminal.css").read_text(encoding="utf-8")
    market = (ASSETS / "assets" / "market-data.js").read_text(encoding="utf-8")
    terminal = (ASSETS / "assets" / "terminal.js").read_text(encoding="utf-8")
    settings = (ASSETS / "assets" / "settings.js").read_text(encoding="utf-8")

    assert "min-width: 20rem" not in style
    assert "--rail-decay" not in terminal
    assert "freshnessRatio" not in market
    assert "freshnessRatio" not in terminal
    assert 'save: el("model-settings-save")' not in settings


def test_removed_workspace_navigation_leaves_no_dead_ui_chain() -> None:
    template = (ASSETS / "templates" / "index.html").read_text(encoding="utf-8")
    product = (ASSETS / "assets" / "product.js").read_text(encoding="utf-8")
    terminal = (ASSETS / "assets" / "terminal.js").read_text(encoding="utf-8")
    style = (ASSETS / "assets" / "terminal.css").read_text(encoding="utf-8")

    assert 'class="workspace-nav"' not in template
    assert "data-workspace-target" not in template + product
    assert "workspace-nav" not in style
    assert '"hashchange"' not in product
    assert "scrollIntoView" not in product
    assert '"stonks:market-failure"' in product
    assert 'new CustomEvent("stonks:market-failure"' in terminal
    assert 'window.addEventListener("stonks:market-failure"' in product
    assert '[data-rail="stale"] .status-badge' in style
    assert "padding-bottom: 5rem" in style
    assert 'placeholder="https:&#47;&#47;api.example.com"' in template
    assert ".dataset.state=" in product
    assert '.closest(".environment")' in product


def test_watchlist_has_no_fixed_twelve_item_ui_cap() -> None:
    terminal = (ASSETS / "assets" / "terminal.js").read_text(encoding="utf-8")

    assert "MAX_WATCHLIST" not in terminal
    assert "/12" not in terminal


def test_research_ui_locks_single_flight_before_post_and_fences_late_results() -> None:
    research = (ASSETS / "assets" / "research.js").read_text(encoding="utf-8")
    start = research.index("async function start")
    post = research.index('jsonRequest("/api/v1/research/runs"', start)

    assert research.index("state.busy = true", start) < post
    assert "if (state.busy)" in research[start:post]
    assert research.count("serial !== state.serial") >= 3
    assert 'new CustomEvent("stonks:research-terminal")' in research
