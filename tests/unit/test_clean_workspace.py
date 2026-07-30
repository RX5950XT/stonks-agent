from __future__ import annotations

from pathlib import Path

import pytest

from scripts.clean_workspace import (
    CleanupError,
    build_cleanup_plan,
    execute_cleanup,
)


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='test'\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return tmp_path


def test_default_plan_only_selects_rebuildable_outputs(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    generated = (
        root / ".mypy_cache",
        root / "output",
        root / "src" / "package" / "__pycache__",
    )
    preserved = (
        root / ".venv",
        root / ".data" / "models",
        root / ".research" / "upstreams",
        root / "src" / "package",
    )
    for directory in generated + preserved:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "sentinel").write_text("x", encoding="utf-8")

    plan = build_cleanup_plan(root)

    assert set(plan.targets) == {path.resolve() for path in generated}
    assert all(path.exists() for path in preserved)


def test_isolated_env_cleanup_keeps_quick_start_environments(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    removed = (
        root / "packages" / "contracts" / ".venv",
        root / "sidecars" / "lean" / ".venv",
        root / "workers" / "tradingagents" / ".venv",
    )
    preserved = (
        root / ".venv",
        root / "sidecars" / "openbb" / ".venv",
        root / "workers" / "kronos" / ".venv",
    )
    for directory in removed + preserved:
        directory.mkdir(parents=True, exist_ok=True)

    plan = build_cleanup_plan(root, include_isolated_envs=True)

    assert set(plan.targets) == {path.resolve() for path in removed}
    assert not set(preserved) & set(plan.targets)


def test_dry_run_preserves_targets_and_cleanup_removes_only_plan(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    cache = root / ".pytest_cache"
    report = root / "coverage.xml"
    protected = root / ".data" / "artifact"
    cache.mkdir()
    protected.parent.mkdir()
    (cache / "cache").write_bytes(b"123")
    report.write_bytes(b"4567")
    protected.write_bytes(b"keep")
    plan = build_cleanup_plan(root)

    dry_run = execute_cleanup(plan, dry_run=True)

    assert dry_run.deleted_count == 0
    assert cache.exists()
    assert report.exists()

    report_result = execute_cleanup(plan, dry_run=False)

    assert report_result.deleted_count == 2
    assert report_result.reclaimed_bytes == 7
    assert not cache.exists()
    assert not report.exists()
    assert protected.read_bytes() == b"keep"


def test_cleanup_rejects_non_project_root(tmp_path: Path) -> None:
    with pytest.raises(CleanupError, match="source checkout"):
        build_cleanup_plan(tmp_path)
