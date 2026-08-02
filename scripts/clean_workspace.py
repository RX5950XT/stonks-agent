"""Remove only allowlisted, rebuildable local workspace outputs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

_GENERATED_PATHS = (
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".hypothesis",
    ".playwright-cli",
    "output",
    ".coverage",
    "coverage.xml",
    "htmlcov",
)
_ISOLATED_ENVS = (
    "packages/contracts/.venv",
    "sidecars/lean/.venv",
    "sidecars/nautilus/.venv",
    "workers/quant_lab/.venv",
    "workers/quant_lab/rd_agent/.venv",
    "workers/tradingagents/.venv",
)
_SOURCE_ROOTS = (
    "migrations",
    "packages",
    "scripts",
    "sidecars",
    "src",
    "strategies",
    "tests",
    "workers",
)


class CleanupError(ValueError):
    """The requested cleanup is outside the frozen safe scope."""


@dataclass(frozen=True)
class CleanupPlan:
    root: Path
    targets: tuple[Path, ...]
    reclaimable_bytes: int


@dataclass(frozen=True)
class CleanupReport:
    root: str
    planned_count: int
    deleted_count: int
    reclaimed_bytes: int
    dry_run: bool


def build_cleanup_plan(
    root: Path,
    *,
    include_isolated_envs: bool = False,
) -> CleanupPlan:
    """Resolve an exact cleanup plan without changing the filesystem."""

    resolved_root = _validated_root(root)
    relative_targets = list(_GENERATED_PATHS)
    if include_isolated_envs:
        relative_targets.extend(_ISOLATED_ENVS)
    targets = {
        candidate
        for relative in relative_targets
        if (candidate := _safe_existing_target(resolved_root, relative)) is not None
    }
    targets.update(_pycache_targets(resolved_root))
    ordered = tuple(sorted(targets, key=lambda path: path.as_posix().casefold()))
    return CleanupPlan(
        root=resolved_root,
        targets=ordered,
        reclaimable_bytes=sum(_target_size(target) for target in ordered),
    )


def execute_cleanup(
    plan: CleanupPlan,
    *,
    dry_run: bool,
) -> CleanupReport:
    """Delete the immutable plan targets or report them without side effects."""

    deleted_count = 0
    if not dry_run:
        for target in plan.targets:
            _validate_planned_target(plan.root, target)
            if not target.exists():
                continue
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            deleted_count += 1
    return CleanupReport(
        root=str(plan.root),
        planned_count=len(plan.targets),
        deleted_count=deleted_count,
        reclaimed_bytes=plan.reclaimable_bytes,
        dry_run=dry_run,
    )


def _validated_root(root: Path) -> Path:
    resolved = root.resolve(strict=True)
    markers = (resolved / "pyproject.toml", resolved / "uv.lock")
    if not all(marker.is_file() for marker in markers):
        raise CleanupError("cleanup root is not a stonks-agent source checkout")
    return resolved


def _safe_existing_target(root: Path, relative: str) -> Path | None:
    candidate = root / relative
    if not candidate.exists() and not candidate.is_symlink():
        return None
    _validate_planned_target(root, candidate)
    return candidate.resolve(strict=True)


def _validate_planned_target(root: Path, target: Path) -> None:
    if target.is_symlink() or target.is_junction():
        raise CleanupError("cleanup target must not be a link")
    resolved = target.resolve(strict=False)
    if resolved == root or not resolved.is_relative_to(root):
        raise CleanupError("cleanup target escaped the source checkout")


def _pycache_targets(root: Path) -> set[Path]:
    targets: set[Path] = set()
    for relative in _SOURCE_ROOTS:
        source_root = root / relative
        if not source_root.is_dir():
            continue
        for current, directories, _files in os.walk(source_root, topdown=True):
            current_path = Path(current)
            retained: list[str] = []
            for name in directories:
                candidate = current_path / name
                if name == "__pycache__":
                    _validate_planned_target(root, candidate)
                    targets.add(candidate.resolve(strict=True))
                elif name != ".venv" and not candidate.is_symlink():
                    retained.append(name)
            directories[:] = retained
    return targets


def _target_size(target: Path) -> int:
    if target.is_file():
        return target.stat().st_size
    size = 0
    for current, directories, files in os.walk(target, topdown=True):
        current_path = Path(current)
        directories[:] = [
            name for name in directories if not (current_path / name).is_symlink()
        ]
        for name in files:
            candidate = current_path / name
            if not candidate.is_symlink():
                size += candidate.stat().st_size
    return size


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--include-isolated-envs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        plan = build_cleanup_plan(
            arguments.root,
            include_isolated_envs=arguments.include_isolated_envs,
        )
        report = execute_cleanup(plan, dry_run=arguments.dry_run)
    except (CleanupError, OSError) as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "status": "rejected",
                    "data": None,
                    "error": {"code": "cleanup_rejected", "message": str(error)},
                },
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "success": True,
                "status": "completed" if not report.dry_run else "planned",
                "data": asdict(report),
                "error": None,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
