from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_checkout_line_endings_are_identical_on_linux_and_windows() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "* text=auto eol=lf" in attributes.splitlines()


def test_nautilus_sbom_cli_is_frozen_in_the_root_dev_lock() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["dependency-groups"]["dev"]
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert any(value.startswith("cyclonedx-bom") for value in dependencies)
    assert "uv run cyclonedx-py environment" in workflow
    assert 'name = "cyclonedx-bom"' in (ROOT / "uv.lock").read_text(encoding="utf-8")
