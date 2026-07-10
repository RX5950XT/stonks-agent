from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "stonks_agent"

FORBIDDEN_BY_LAYER = {
    "domain": (
        "fastapi",
        "sqlalchemy",
        "stonks_agent.adapters",
        "stonks_agent.application",
        "stonks_agent.config",
        "stonks_agent.entrypoints",
        "stonks_agent.ports",
    ),
    "ports": (
        "fastapi",
        "sqlalchemy",
        "stonks_agent.adapters",
        "stonks_agent.application",
        "stonks_agent.config",
        "stonks_agent.entrypoints",
    ),
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_dependency_direction_is_enforced() -> None:
    violations: list[str] = []
    for layer, forbidden_prefixes in FORBIDDEN_BY_LAYER.items():
        for path in sorted((SRC / layer).rglob("*.py")):
            for module in imported_modules(path):
                if module.startswith(forbidden_prefixes):
                    violations.append(f"{path.relative_to(ROOT)} imports {module}")

    assert violations == []
