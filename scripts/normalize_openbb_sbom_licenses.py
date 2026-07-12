#!/usr/bin/env python3
"""Normalize OpenBB SBOM licenses from the reviewed frozen inventory."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip().lower())


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return cast(Mapping[str, Any], value)


def _load(path: Path, *, yaml_format: bool) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    value = yaml.safe_load(text) if yaml_format else json.loads(text)
    return _mapping(value, str(path))


def normalized_sbom(root: Path) -> str:
    """Return canonical JSON with policy-bound SPDX expressions."""

    sidecar = root / "sidecars" / "openbb"
    sbom = dict(_load(sidecar / "sbom.cdx.json", yaml_format=False))
    policy = _load(sidecar / "license-policy.yaml", yaml_format=True)
    inventory = _mapping(policy.get("components"), "license policy components")
    raw_components = sbom.get("components")
    if not isinstance(raw_components, list):
        raise ValueError("SBOM components must be a list")
    components = [dict(_mapping(item, "SBOM component")) for item in raw_components]
    actual_names = {_normalize_name(str(item.get("name"))) for item in components}
    if actual_names != set(inventory):
        raise ValueError("license inventory must exactly cover SBOM components")
    for component in components:
        name = _normalize_name(str(component.get("name")))
        expected = _mapping(inventory[name], f"license policy component {name}")
        if component.get("version") != expected.get("version"):
            raise ValueError(f"version drift for {name}")
        expression = expected.get("expression")
        if not isinstance(expression, str) or not expression:
            raise ValueError(f"missing SPDX expression for {name}")
        component["licenses"] = [
            {"acknowledgement": "declared", "expression": expression}
        ]
    sbom["components"] = components
    return json.dumps(sbom, ensure_ascii=False, indent=2) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    path = args.root / "sidecars" / "openbb" / "sbom.cdx.json"
    try:
        normalized = normalized_sbom(args.root)
        if args.check:
            return 0 if path.read_text(encoding="utf-8") == normalized else 1
        path.write_text(normalized, encoding="utf-8")
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        yaml.YAMLError,
    ) as error:
        print(f"OpenBB SBOM license normalization failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
