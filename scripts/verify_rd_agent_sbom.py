#!/usr/bin/env python3
"""Verify the RD factor sandbox CycloneDX runtime inventory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REQUIRED = frozenset(
    {
        ("annotated-types", "0.7.0"),
        ("pydantic", "2.12.5"),
        ("pydantic-core", "2.41.5"),
        ("python", "3.12.13"),
        ("pyyaml", "6.0.3"),
        ("stonks-contracts", "0.1.0"),
        ("typing-extensions", "4.16.0"),
        ("typing-inspection", "0.4.2"),
    }
)
FORBIDDEN = frozenset(
    {
        "docker",
        "numpy",
        "openbb",
        "pandas",
        "pip",
        "psycopg",
        "pyqlib",
        "qlib",
        "rd-agent",
        "rdagent",
        "sqlite-libs",
        "torch",
        "uv",
    }
)


def verify(path: Path) -> tuple[int, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.6":
        raise ValueError("RD factor sandbox SBOM schema changed")
    components = payload.get("components")
    if not isinstance(components, list):
        raise ValueError("RD factor sandbox SBOM inventory is incomplete")
    packages = _packages(components)
    missing = sorted(REQUIRED - packages)
    if missing:
        raise ValueError(f"RD factor SBOM required packages missing: {missing}")
    forbidden = sorted(name for name, _ in packages if name in FORBIDDEN)
    if forbidden:
        raise ValueError(f"RD factor SBOM forbidden packages present: {forbidden}")
    return len(components), len(packages)


def _packages(components: list[object]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for raw in components:
        if not isinstance(raw, dict):
            raise ValueError("RD factor sandbox SBOM component is invalid")
        item: dict[str, Any] = raw
        name, version = item.get("name"), item.get("version")
        if isinstance(name, str) and isinstance(version, str):
            result.add((name.replace("_", "-").lower(), version))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sbom", type=Path)
    args = parser.parse_args()
    components, packages = verify(args.sbom)
    print(json.dumps({"components": components, "unique_packages": packages}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
