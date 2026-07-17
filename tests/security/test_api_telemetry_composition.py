from __future__ import annotations

import ast
from pathlib import Path


def test_every_fastapi_factory_installs_outer_telemetry_after_security() -> None:
    route_root = Path("src/stonks_agent/entrypoints/api/routes")
    expected = {
        "create_data_app",
        "create_paper_operations_app",
        "create_paper_projection_app",
        "create_research_app",
        "create_strategy_app",
    }
    composed: set[str] = set()

    for path in route_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in expected:
                continue
            calls = [
                child.func.id
                for child in node.body
                if isinstance(child, ast.Expr)
                and isinstance(child.value, ast.Call)
                and isinstance(child.value.func, ast.Name)
                for child in (child.value,)
            ]
            if (
                "install_api_security" in calls
                and "install_api_telemetry" in calls
                and calls.index("install_api_security")
                < calls.index("install_api_telemetry")
            ):
                composed.add(node.name)

    assert composed == expected
