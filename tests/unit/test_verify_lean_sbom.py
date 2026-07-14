from __future__ import annotations

import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location(
    "verify_lean_sbom_under_test", ROOT / "scripts" / "verify_lean_sbom.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
REQUIRED: frozenset[tuple[str, str]] = MODULE.REQUIRED
verify: Any = MODULE.verify


def _write_sbom(path: Path, *, forbidden: bool = False) -> None:
    components = [
        {"type": "library", "name": name, "version": version}
        for name, version in sorted(REQUIRED)
    ]
    components.extend(
        {"type": "library", "name": f"filler-{index}", "version": "1"}
        for index in range(100)
    )
    if forbidden:
        components.append({"type": "library", "name": "DotNetZip", "version": "1.16"})
    path.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "components": components,
            }
        ),
        encoding="utf-8",
    )


def test_verify_lean_sbom_requires_complete_pinned_inventory(tmp_path: Path) -> None:
    target = tmp_path / "sbom.json"
    _write_sbom(target)

    components, packages = verify(target)

    assert components == len(REQUIRED) + 100
    assert packages == len(REQUIRED) + 100


def test_verify_lean_sbom_rejects_removed_dependency_chain(tmp_path: Path) -> None:
    target = tmp_path / "sbom.json"
    _write_sbom(target, forbidden=True)

    with pytest.raises(ValueError, match="forbidden packages"):
        verify(target)
