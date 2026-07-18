from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = ROOT / "src" / "stonks_agent" / "adapters" / "artifacts"


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_sigv4_transport_has_no_default_credential_or_sdk_io_chain() -> None:
    source = (ADAPTERS / "s3_http.py").read_text(encoding="utf-8")
    imported = imports(ADAPTERS / "s3_http.py")

    assert "boto3" not in imported
    assert "botocore.session" not in imported
    assert "os" not in imported
    assert "Session(" not in source
    assert "InstanceMetadata" not in source
    assert "ContainerMetadata" not in source
    assert "AWS_ACCESS_KEY_ID" not in source
    assert "AWS_SECRET_ACCESS_KEY" not in source


def test_artifact_maintenance_has_no_trade_or_canonical_delete_authority() -> None:
    imported = imports(ADAPTERS / "s3_maintenance.py")
    source = (ADAPTERS / "s3_maintenance.py").read_text(encoding="utf-8")

    for forbidden in (
        "execution",
        "ledger",
        "order",
        "portfolio",
        "reservation",
        "risk",
        "trading",
    ):
        assert all(forbidden not in module for module in imported)
    assert "BypassGovernanceRetention" not in source
    assert "delete_bucket" not in source
    assert "put_object(" not in source


def test_test_runtime_is_not_part_of_default_or_optional_production_compose() -> None:
    default_manifests = (
        ROOT / "infra" / "compose.test.yaml",
        ROOT / "infra" / "compose.optional.yaml",
    )
    for path in default_manifests:
        content = path.read_text(encoding="utf-8")
        assert "seaweedfs" not in content.lower()
        assert "s3-compatible" not in content
