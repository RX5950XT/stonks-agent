from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_exported_schema_snapshots_are_current() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "export_schemas.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_schema_tree_does_not_expose_trade_intent() -> None:
    schema_files = tuple((ROOT / "schemas" / "v1").glob("*.json"))

    assert schema_files
    assert all(
        "TradeIntent" not in path.read_text(encoding="utf-8") for path in schema_files
    )
