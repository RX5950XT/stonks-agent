from __future__ import annotations

import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location(
    "verify_rd_agent_sbom_under_test",
    ROOT / "scripts" / "verify_rd_agent_sbom.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
REQUIRED: frozenset[tuple[str, str]] = MODULE.REQUIRED
verify: Any = MODULE.verify


def _write_sbom(
    path: Path,
    *,
    missing: tuple[str, str] | None = None,
    forbidden: str | None = None,
) -> None:
    components = [
        {"type": "library", "name": name, "version": version}
        for name, version in sorted(REQUIRED - ({missing} if missing else set()))
    ]
    if forbidden is not None:
        components.append({"type": "library", "name": forbidden, "version": "1.0"})
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


def test_verify_rd_agent_sbom_accepts_exact_minimal_runtime(tmp_path: Path) -> None:
    target = tmp_path / "sbom.json"
    _write_sbom(target)

    components, packages = verify(target)

    assert components == len(REQUIRED)
    assert packages == len(REQUIRED)


def test_verify_rd_agent_sbom_rejects_missing_or_forbidden_runtime(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    forbidden = tmp_path / "forbidden.json"
    _write_sbom(missing, missing=("pydantic", "2.12.5"))
    _write_sbom(forbidden, forbidden="sqlite-libs")

    with pytest.raises(ValueError, match="required packages missing"):
        verify(missing)
    with pytest.raises(ValueError, match="forbidden packages present"):
        verify(forbidden)
