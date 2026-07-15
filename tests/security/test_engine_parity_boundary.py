from __future__ import annotations

import ast
from pathlib import Path

from stonks_agent.application.evaluation.engine_parity import EngineParityRequest
from stonks_agent.domain.engine_parity import EngineParityReport

ROOT = Path(__file__).resolve().parents[2]
PARITY_SOURCES = (
    ROOT / "src" / "stonks_agent" / "domain" / "engine_parity.py",
    ROOT / "src" / "stonks_agent" / "application" / "evaluation" / "engine_parity.py",
)


def test_engine_parity_core_has_no_sidecar_paper_or_persistence_authority() -> None:
    forbidden = (
        "sidecars",
        "nautilus_trader",
        "QuantConnect",
        "stonks_agent.adapters.postgres",
        "stonks_agent.application.execution",
        "stonks_agent.application.ledger",
        "stonks_agent.application.portfolio",
        "stonks_agent.application.risk",
        "stonks_agent.ports.execution",
        "stonks_agent.ports.ledger",
        "stonks_agent.ports.repository",
        "stonks_agent.ports.unit_of_work",
    )

    for path in PARITY_SOURCES:
        tree = ast.parse(path.read_text("utf-8"))
        imports = tuple(_imports(tree))
        assert not any(
            name == prefix or name.startswith(f"{prefix}.")
            for name in imports
            for prefix in forbidden
        )


def test_engine_parity_schema_cannot_select_average_or_authorize_engine() -> None:
    fields = frozenset(EngineParityReport.model_fields) | frozenset(
        EngineParityRequest.model_fields
    )
    forbidden = {
        "average",
        "averaged_result",
        "order",
        "passed_for_promotion",
        "preferred_engine",
        "risk_decision",
        "selected_engine",
        "target",
    }

    assert fields.isdisjoint(forbidden)
    assert (
        "warnings"
        not in EngineParityReport.model_json_schema()["$defs"]["EngineResultEvidence"][
            "properties"
        ]
    )


def _imports(tree: ast.AST) -> tuple[str, ...]:
    result: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            result.append(node.module)
        elif isinstance(node, ast.Import):
            result.extend(item.name for item in node.names)
    return tuple(result)
