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


def test_gui_exposes_the_backend_driven_primary_journey_without_terminal_commands() -> (
    None
):
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
    assert 'autocomplete="new-password"' in template
    assert 'id="model-settings-save"' in template
    assert 'id="model-settings-clear"' in template
    assert 'aria-describedby="research-action-note"' in template
    assert 'id="research-progress"' in template
    assert 'id="research-results"' in template
    assert 'id="research-report"' in template
    assert 'id="research-history"' in template
    assert 'id="research-evidence"' in template
    assert 'id="market-table-body"' in template
    assert 'id="expert-console"' in template
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
    assert "paper.portfolio" in product
    assert "paper.risk" in product
    assert "paper.safety" in product
    assert "nav.cash_value" in product
    assert "nav.position_value" in product
    assert "nav.cumulative_fees" in product
    assert "nav.realized_pnl" in product
    assert "position.sellable" in product
    assert "position.reserved" in product
    assert "risk.decided_at" in product
    assert "integrity.ledger_hash" in product
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
    assert "view.freshness" in terminal
    assert "view.quality" in terminal
    assert 'view.is_real_time ? "即時" : "非 tick"' in terminal
    assert "staleAfter" not in terminal
    assert "kronos_summary" not in research


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


def test_research_ui_locks_single_flight_before_post_and_fences_late_results() -> None:
    research = (ASSETS / "assets" / "research.js").read_text(encoding="utf-8")
    start = research.index("async function start")
    post = research.index('jsonRequest("/api/v1/research/runs"', start)

    assert research.index("state.busy = true", start) < post
    assert "if (state.busy)" in research[start:post]
    assert research.count("serial !== state.serial") >= 3
    assert 'new CustomEvent("stonks:research-terminal")' in research
