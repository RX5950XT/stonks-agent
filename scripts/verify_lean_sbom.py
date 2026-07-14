#!/usr/bin/env python3
"""Verify the combined LEAN image CycloneDX inventory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REQUIRED = frozenset(
    {
        ("fastapi", "0.139.0"),
        ("pydantic", "2.12.5"),
        ("quantconnect.lean.launcher", "1.0.0"),
        ("sharpziplib", "1.4.2"),
        ("stonks.lean.algorithm", "1.0.0.0"),
        ("uvicorn", "0.51.0"),
    }
)
FORBIDDEN = frozenset(
    {
        "dotnetzip",
        "netmq",
        "system.drawing.common",
        "system.net.http.winhttphandler",
        "system.private.servicemodel",
        "system.servicemodel.primitives",
    }
)


def verify(path: Path) -> tuple[int, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.6":
        raise ValueError("LEAN SBOM schema changed")
    components = payload.get("components")
    if not isinstance(components, list) or len(components) < 100:
        raise ValueError("LEAN SBOM inventory is incomplete")
    packages = _packages(components)
    missing = sorted(REQUIRED - packages)
    if missing:
        raise ValueError(f"LEAN SBOM required packages missing: {missing}")
    forbidden = sorted(name for name, _ in packages if name in FORBIDDEN)
    if forbidden:
        raise ValueError(f"LEAN SBOM forbidden packages present: {forbidden}")
    return len(components), len(packages)


def _packages(components: list[object]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for raw in components:
        if not isinstance(raw, dict):
            raise ValueError("LEAN SBOM component is invalid")
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
