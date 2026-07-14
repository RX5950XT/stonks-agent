from __future__ import annotations

import ast
from pathlib import Path

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.platform import (
    AI_TRADER_ENDPOINT_TEMPLATES,
    AiTraderHttpAdapter,
    MemoryPlatformEventInbox,
)
from stonks_agent.ports.platform import PlatformPort

ROOT = Path(__file__).parents[2]


def test_ai_trader_adapter_has_no_control_plane_dependency() -> None:
    path = ROOT / "src" / "stonks_agent" / "adapters" / "platform" / "ai_trader.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(
        token in module
        for module in imports
        for token in (
            "adapters.postgres",
            "domain.orders",
            "domain.reservations",
            "domain.risk",
            "ports.execution",
            "ports.queue",
        )
    )


def test_ai_trader_adapter_structurally_satisfies_research_only_platform_port() -> None:
    adapter = AiTraderHttpAdapter(
        client=object(),  # type: ignore[arg-type]
        artifacts=MemoryArtifactStore(),
        event_inbox=MemoryPlatformEventInbox(),
        access_token="opaque-token",
    )

    assert isinstance(adapter, PlatformPort)
    assert not hasattr(adapter, "submit_order")
    assert not hasattr(adapter, "copy_trade")
    assert all("trade" not in endpoint for endpoint in AI_TRADER_ENDPOINT_TEMPLATES)
