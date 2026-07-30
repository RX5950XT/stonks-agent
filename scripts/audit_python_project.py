#!/usr/bin/env python3
"""Audit one frozen Python project, including non-PyPI local-build identities."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def locked_public_version(lock_path: Path, package_name: str) -> str:
    """Return one public PEP 440 identity for all locked local-build variants."""
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    versions = {
        str(package["version"]).partition("+")[0]
        for package in lock.get("package", ())
        if str(package.get("name", "")).lower() == package_name.lower()
    }
    if len(versions) != 1:
        raise ValueError(
            f"{package_name} must resolve to exactly one public locked version"
        )
    return versions.pop()


def export_command(
    uv: str,
    project: Path,
    requirements: Path,
    standard_identity_packages: Sequence[str],
) -> tuple[str, ...]:
    command = [
        uv,
        "export",
        "--project",
        str(project),
        "--quiet",
        "--frozen",
        "--no-dev",
        "--no-emit-project",
        "--no-emit-local",
    ]
    for package in standard_identity_packages:
        command.extend(("--no-emit-package", package))
    command.extend(("--format", "requirements.txt", "--output-file", str(requirements)))
    return tuple(command)


def audit_command(requirements: Path, *, no_deps: bool = False) -> tuple[str, ...]:
    command = [sys.executable, "-m", "pip_audit", "--strict"]
    if no_deps:
        command.extend(("--no-deps", "--disable-pip"))
    command.extend(("--requirement", str(requirements)))
    return tuple(command)


def audit_project(
    project: Path,
    standard_identity_packages: Sequence[str],
) -> int:
    uv = shutil.which("uv")
    if uv is None:
        print("[audit-python-project] failed: uv executable not found")
        return 1
    resolved = project.resolve()
    if not resolved.is_relative_to(ROOT) or not (resolved / "uv.lock").is_file():
        print("[audit-python-project] failed: project must contain a lock inside repo")
        return 2
    with TemporaryDirectory(prefix="stonks-project-audit-") as directory:
        temporary = Path(directory)
        requirements = temporary / "requirements.txt"
        export = export_command(
            uv,
            resolved,
            requirements,
            standard_identity_packages,
        )
        if subprocess.run(export, cwd=ROOT, check=False).returncode != 0:
            return 1
        if (
            subprocess.run(
                audit_command(requirements), cwd=ROOT, check=False
            ).returncode
            != 0
        ):
            return 1
        for package in standard_identity_packages:
            public_version = locked_public_version(resolved / "uv.lock", package)
            identity = temporary / f"{package}-public.txt"
            identity.write_text(
                f"{package}=={public_version}\n",
                encoding="utf-8",
            )
            print(
                f"[audit-python-project] audit {package} public identity "
                f"{public_version}",
                flush=True,
            )
            if (
                subprocess.run(
                    audit_command(identity, no_deps=True),
                    cwd=ROOT,
                    check=False,
                ).returncode
                != 0
            ):
                return 1
    print(f"[audit-python-project] passed: {resolved.relative_to(ROOT)}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument(
        "--standard-identity-package",
        action="append",
        default=[],
        help="audit a local-build package separately using its public version",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    return audit_project(args.project, tuple(args.standard_identity_package))


if __name__ == "__main__":
    raise SystemExit(main())
