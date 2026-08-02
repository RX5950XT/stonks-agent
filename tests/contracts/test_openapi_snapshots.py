from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location(
    "export_openapi_under_test",
    ROOT / "scripts" / "export_openapi.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
build_documents: Any = MODULE.build_documents


def test_openapi_snapshots_match_deterministic_reference_contracts() -> None:
    documents = build_documents()

    assert set(documents) == {
        "data.openapi.json",
        "deployment.openapi.json",
        "gui.openapi.json",
        "paper-operations.openapi.json",
        "paper-projections.openapi.json",
        "research.openapi.json",
        "strategies.openapi.json",
    }
    for name, document in documents.items():
        expected = json.loads(
            (ROOT / "schemas" / "openapi" / "v1" / name).read_text(encoding="utf-8")
        )
        assert document == expected
        assert document["openapi"] == "3.1.0"
        expected_version = "0.2.0" if name == "gui.openapi.json" else "0.1.0"
        assert document["info"]["version"] == expected_version
        assert document["x-stonks-execution-mode"] == "paper"
        if name == "deployment.openapi.json":
            assert document["x-stonks-surface"] == "deployed-health-reference"
        elif name == "gui.openapi.json":
            assert document["x-stonks-surface"] == "local-actual-runtime"
            assert document["x-stonks-authority"] == {
                "mode": "bounded_research_command",
                "runtime": "not_composed_by_default",
                "trading": "canonical_paper_only",
            }
        else:
            assert document["x-stonks-surface"] == "reference-contract-only"
    gui_responses = documents["gui.openapi.json"]["paths"][
        "/api/v1/market-data/latest"
    ]["get"]["responses"]
    assert gui_responses["200"]["content"]["application/json"]["schema"]
    assert "422" not in gui_responses
    gui_paths = documents["gui.openapi.json"]["paths"]
    assert set(gui_paths["/api/v1/research/runs"]) == {"get", "post"}
    assert set(gui_paths["/api/v1/research/runs/{run_id}"]) == {"get"}
    assert set(gui_paths["/api/v1/research/runs/{run_id}/evidence"]) == {"get"}
    assert set(gui_paths["/api/v1/research/runs/{run_id}/events"]) == {"get"}
    for path in (
        "/api/v1/research/runs",
        "/api/v1/research/runs/{run_id}",
        "/api/v1/research/runs/{run_id}/events",
    ):
        operation = next(iter(gui_paths[path].values()))
        assert "422" not in operation["responses"]
    sse = gui_paths["/api/v1/research/runs/{run_id}/events"]["get"]["responses"]["200"]
    assert "text/event-stream" in sse["content"]
