from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SIDECAR = ROOT / "sidecars" / "nautilus"


def test_nautilus_runtime_is_isolated_from_core_and_authority_ports() -> None:
    core_dependency_files = (
        (ROOT / "pyproject.toml").read_text("utf-8"),
        (ROOT / "uv.lock").read_text("utf-8"),
    )
    forbidden_imports = {
        "stonks_agent.adapters.postgres",
        "stonks_agent.domain.risk",
        "stonks_agent.ports.execution",
        "stonks_agent.ports.ledger",
        "stonks_agent.ports.repository",
        "stonks_agent.ports.unit_of_work",
    }

    assert all('name = "nautilus-trader"' not in item for item in core_dependency_files)
    for path in SIDECAR.glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.add(node.module)
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
        assert imports.isdisjoint(forbidden_imports)
        assert not any(
            name == "stonks_agent" or name.startswith("stonks_agent.")
            for name in imports
        )
        source = path.read_text("utf-8").lower()
        assert not any(
            marker in source
            for marker in ("database_url", "postgresql", "redis_url", "broker_token")
        )
    assert "nautilus_trader" not in (SIDECAR / "adapter.py").read_text("utf-8")
    assert "nautilus_trader" not in (SIDECAR / "app.py").read_text("utf-8")


def test_nautilus_lock_docker_and_lgpl_notice_are_pinned() -> None:
    project = (SIDECAR / "pyproject.toml").read_text("utf-8")
    lock = (SIDECAR / "uv.lock").read_text("utf-8")
    dockerfile = (SIDECAR / "Dockerfile").read_text("utf-8")
    notice = (SIDECAR / "NOTICE.md").read_text("utf-8")
    manifest = yaml.safe_load(
        (ROOT / "docs" / "legal" / "upstream-manifest.yaml").read_text("utf-8")
    )
    upstream = next(
        item for item in manifest["upstreams"] if item["id"] == "nautilus-trader"
    )

    assert '"nautilus_trader==1.230.0"' in project
    assert re.search(r'name = "nautilus-trader"\s+version = "1\.230\.0"', lock)
    assert "USER 65532:65532" in dockerfile
    assert "NAUTILUS-LGPL-3.0" in dockerfile
    assert "LGPL-3.0-or-later" in notice
    assert "replace the dynamic wheel" in notice
    assert "GNU-GPL-3.0" in dockerfile
    assert "/usr/share/source/nautilus-trader" in dockerfile
    assert upstream["snapshot"] == "8160730c7c550480b0a439fb11086a4c4de15f0b"
    assert upstream["license"]["expression"] == "LGPL-3.0-or-later"
    assert upstream["adoption"]["in_core_allowed"] is False

    distribution = yaml.safe_load(
        (SIDECAR / "distribution-manifest.yaml").read_text("utf-8")
    )
    assert distribution["source_review"]["commit"] == upstream["snapshot"]
    assert (
        distribution["source_review"]["license_sha256"]
        == upstream["license"]["evidence"][0]["sha256"]
    )
    for artifact in distribution["published_artifacts"].values():
        assert artifact["url"] in lock
        assert f"sha256:{artifact['sha256']}" in lock


def test_runtime_hash_covers_adapter_engine_http_and_lock() -> None:
    from sidecars.nautilus.adapter import compute_runtime_hash

    runtime_hash = compute_runtime_hash(SIDECAR)

    assert re.fullmatch(r"[0-9a-f]{64}", runtime_hash)
