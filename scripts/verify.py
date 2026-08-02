#!/usr/bin/env python3
"""Run the reproducible P0 quality, security and governance gates."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
_PROFILE_AUDITS = (
    ("workers/tradingagents", ()),
    ("workers/kronos", ("--standard-identity-package", "torch")),
    (
        "workers/kronos/profiles/cuda",
        ("--standard-identity-package", "torch"),
    ),
    ("workers/quant_lab", ()),
    ("workers/quant_lab/rd_agent", ()),
    ("sidecars/openbb", ()),
    ("sidecars/nautilus", ()),
    ("sidecars/lean", ()),
)


def commands(*, with_postgres: bool) -> tuple[tuple[str, ...], ...]:
    python = sys.executable
    pytest_command: tuple[str, ...] = (python, "-m", "pytest", "-q")
    if not with_postgres:
        pytest_command += (
            "-m",
            "not postgres",
            "--cov-config=.coveragerc.core",
        )
    checks: list[tuple[str, ...]] = [
        (python, "-m", "ruff", "format", "--check", "."),
        (python, "-m", "ruff", "check", "."),
        (python, "-m", "mypy", "src", "packages"),
        pytest_command,
        (python, "scripts/export_schemas.py", "--check"),
        (python, "scripts/check_upstream_policy.py"),
        (python, "scripts/check_secrets.py"),
    ]
    if with_postgres:
        checks.append((python, "-m", "alembic", "check"))
    return tuple(checks)


def run(*, skip_audit: bool, with_postgres: bool) -> int:
    environment = os.environ.copy()
    environment.setdefault("PYTHONUTF8", "1")
    if with_postgres:
        database_url = environment.get("STONKS_TEST_DATABASE_URL")
        if not database_url:
            print("[verify] failed: STONKS_TEST_DATABASE_URL is required")
            return 2
        environment["STONKS_DATABASE_URL"] = database_url
    for command in commands(with_postgres=with_postgres):
        printable = " ".join(command[1:])
        print(f"\n[verify] {printable}", flush=True)
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            check=False,
        )
        if result.returncode != 0:
            print(f"[verify] failed ({result.returncode}): {printable}")
            return result.returncode
    if not skip_audit:
        audit_result = _audit_dependencies(environment)
        if audit_result != 0:
            return audit_result
    print("\n[verify] all gates passed")
    return 0


def _audit_dependencies(environment: dict[str, str]) -> int:
    uv = shutil.which("uv")
    if uv is None:
        print("[verify] failed: uv executable not found")
        return 1
    with TemporaryDirectory(prefix="stonks-audit-") as directory:
        requirements = Path(directory) / "requirements.txt"
        export = (
            uv,
            "export",
            "--quiet",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-emit-workspace",
            "--format",
            "requirements.txt",
            "--output-file",
            str(requirements),
        )
        print("\n[verify] export locked runtime dependencies", flush=True)
        exported = subprocess.run(export, cwd=ROOT, env=environment, check=False)
        if exported.returncode != 0:
            return exported.returncode
        audit = (
            sys.executable,
            "-m",
            "pip_audit",
            "--strict",
            "--requirement",
            str(requirements),
        )
        print("\n[verify] audit locked runtime dependencies", flush=True)
        audited = subprocess.run(audit, cwd=ROOT, env=environment, check=False)
        if audited.returncode != 0:
            return audited.returncode
    for command in profile_audit_commands(sys.executable):
        project = command[command.index("--project") + 1]
        print(f"\n[verify] audit isolated runtime: {project}", flush=True)
        audited = subprocess.run(command, cwd=ROOT, env=environment, check=False)
        if audited.returncode != 0:
            return audited.returncode
    return 0


def profile_audit_commands(python: str) -> tuple[tuple[str, ...], ...]:
    """Return frozen audit commands for every isolated Python runtime lock."""

    return tuple(
        (
            python,
            "scripts/audit_python_project.py",
            "--project",
            project,
            *extra,
        )
        for project, extra in _PROFILE_AUDITS
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-audit",
        action="store_true",
        help="skip only the network-backed dependency vulnerability audit",
    )
    parser.add_argument(
        "--with-postgres",
        action="store_true",
        help="run real PostgreSQL integration tests and migration drift check",
    )
    args = parser.parse_args()
    return run(skip_audit=args.skip_audit, with_postgres=args.with_postgres)


if __name__ == "__main__":
    raise SystemExit(main())
