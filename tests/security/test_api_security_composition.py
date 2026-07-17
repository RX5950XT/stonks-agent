from __future__ import annotations

import ast
from pathlib import Path


def test_every_fastapi_factory_uses_central_security_composition() -> None:
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
            called = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            if "install_api_security" in called:
                composed.add(node.name)

    assert composed == expected
