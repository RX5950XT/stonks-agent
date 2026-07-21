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
        assert document["x-stonks-execution-mode"] == "paper"
        if name == "deployment.openapi.json":
            assert document["x-stonks-surface"] == "deployed-health-reference"
        else:
            assert document["x-stonks-surface"] == "reference-contract-only"
