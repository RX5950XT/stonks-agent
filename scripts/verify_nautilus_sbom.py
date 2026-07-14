#!/usr/bin/env python3
"""Verify a generated CycloneDX inventory for the frozen Nautilus runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _license_ids(component: dict[str, Any]) -> set[str]:
    result = set()
    for item in component.get("licenses", []):
        license_data = item.get("license", {})
        value = license_data.get("id") or license_data.get("name")
        if isinstance(value, str) and value.strip():
            result.add(value.strip())
        expression = item.get("expression")
        if isinstance(expression, str) and expression.strip():
            result.add(expression.strip())
    return result


def verify(path: Path) -> tuple[int, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.6":
        raise ValueError("Nautilus SBOM schema changed")
    components = payload.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("Nautilus SBOM has no components")
    indexed = {
        str(item.get("name", "")).replace("_", "-").lower(): item for item in components
    }
    nautilus = indexed.get("nautilus-trader")
    if nautilus is None or nautilus.get("version") != "1.230.0":
        raise ValueError("Nautilus SBOM package pin changed")
    if "LGPL-3.0-or-later" not in _license_ids(nautilus):
        raise ValueError("Nautilus SBOM LGPL license is missing")
    missing = sorted(
        name for name, component in indexed.items() if not _license_ids(component)
    )
    if missing:
        raise ValueError(f"Nautilus SBOM license metadata missing: {missing}")
    return len(components), len(indexed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sbom", type=Path)
    args = parser.parse_args()
    components, unique = verify(args.sbom)
    print(json.dumps({"components": components, "unique_packages": unique}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
