from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OPENAPI_ROOT = ROOT / "schemas" / "openapi" / "v1"
EXPECTED_SURFACES = {
    "data.openapi.json": "Stonks Agent Data API",
    "deployment.openapi.json": "Stonks Agent Deployment Health",
    "gui.openapi.json": "Stonks Terminal",
    "paper-operations.openapi.json": "Stonks Agent Paper Operations API",
    "paper-projections.openapi.json": "Stonks Agent Paper Projection API",
    "research.openapi.json": "Stonks Agent Research API",
    "strategies.openapi.json": "Stonks Agent Strategy API",
}


@pytest.mark.policy
def test_api_index_exactly_tracks_exported_openapi_surfaces() -> None:
    index = (ROOT / "docs" / "api" / "README.md").read_text(encoding="utf-8")
    snapshots = {path.name for path in OPENAPI_ROOT.glob("*.json")}

    assert snapshots == EXPECTED_SURFACES.keys()
    for filename, title in EXPECTED_SURFACES.items():
        payload = json.loads((OPENAPI_ROOT / filename).read_text(encoding="utf-8"))
        assert payload["openapi"] == "3.1.0"
        version = "0.2.0" if filename == "gui.openapi.json" else "0.1.0"
        assert payload["info"] == {"title": title, "version": version}
        assert index.count(f"../../schemas/openapi/v1/{filename}") == 1
        assert index.count(f"`{title}`") == 1
        for route in payload["paths"]:
            assert index.count(f"`{route}`") == 1


@pytest.mark.policy
def test_api_docs_preserve_runtime_security_and_composition_boundaries() -> None:
    index = (ROOT / "docs" / "api" / "README.md").read_text(encoding="utf-8")

    for permission in ("read", "run_research", "review_strategy", "operate_paper"):
        assert f"`{permission}`" in index
    for token in (
        "success/status/data/error/metadata",
        "Last-Event-ID",
        "text/event-stream",
        "paper-only",
        "未組合成 production business API",
        "六份 business/health snapshot 未內嵌 OpenAPI",
        "Browser/JSON route 沒有人類 auth",
        "GUI → OpenBB sidecar",
    ):
        assert token in index


@pytest.mark.policy
def test_schema_index_links_api_contract_and_generation_check() -> None:
    schema_index = (ROOT / "schemas" / "README.md").read_text(encoding="utf-8")

    assert "../docs/api/README.md" in schema_index
    assert "scripts/export_schemas.py --check" in schema_index
    assert "七份 OpenAPI 3.1 snapshots" in schema_index
