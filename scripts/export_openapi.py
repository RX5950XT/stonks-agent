#!/usr/bin/env python3
"""Export deterministic reference OpenAPI contracts for every core API surface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from stonks_agent.entrypoints.api.deployment import create_deployment_app
from stonks_agent.entrypoints.api.gui import create_gui_app
from stonks_agent.entrypoints.api.routes.data import create_data_app
from stonks_agent.entrypoints.api.routes.operations import create_paper_operations_app
from stonks_agent.entrypoints.api.routes.projections import (
    create_paper_projection_app,
)
from stonks_agent.entrypoints.api.routes.research import create_research_app
from stonks_agent.entrypoints.api.routes.strategies import create_strategy_app


class _NeverCalled:
    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"OpenAPI export attempted to call a port: {name}")


def build_documents() -> dict[str, dict[str, Any]]:
    port = cast(Any, _NeverCalled())
    applications = {
        "data.openapi.json": create_data_app(port),
        "deployment.openapi.json": create_deployment_app(
            port,
            build_revision="0" * 40,
        ),
        "gui.openapi.json": create_gui_app(port),
        "paper-operations.openapi.json": create_paper_operations_app(port),
        "paper-projections.openapi.json": create_paper_projection_app(port),
        "research.openapi.json": create_research_app(port, port, port),
        "strategies.openapi.json": create_strategy_app(port),
    }
    documents: dict[str, dict[str, Any]] = {}
    for name, application in sorted(applications.items()):
        document = application.openapi()
        document["x-stonks-execution-mode"] = "paper"
        document["x-stonks-surface"] = _surface(name)
        if name == "gui.openapi.json":
            document["x-stonks-authority"] = {
                "mode": "bounded_research_command",
                "runtime": "not_composed_by_default",
                "trading": "canonical_paper_only",
            }
        documents[name] = _canonical(document)
    return documents


def _surface(name: str) -> str:
    if name == "deployment.openapi.json":
        return "deployed-health-reference"
    if name == "gui.openapi.json":
        return "local-actual-runtime"
    return "reference-contract-only"


def export(directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("OpenAPI output must be a regular directory")
    documents = build_documents()
    for name, document in documents.items():
        target = directory / name
        target.write_text(
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return len(documents)


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "schemas" / "openapi" / "v1",
    )
    args = parser.parse_args()
    count = export(args.output)
    print(json.dumps({"success": True, "documents": count}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
