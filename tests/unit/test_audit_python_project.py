from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts.audit_python_project import (
    audit_command,
    export_command,
    locked_public_version,
)
from scripts.verify import profile_audit_commands


def test_public_identity_normalizes_cpu_and_standard_locked_versions(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
version = 1
revision = 3
requires-python = ">=3.12,<3.13"

[[package]]
name = "torch"
version = "2.13.0"

[[package]]
name = "torch"
version = "2.13.0+cpu"
""",
        encoding="utf-8",
    )

    assert locked_public_version(lock, "torch") == "2.13.0"


def test_public_identity_rejects_divergent_locked_versions(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
version = 1
revision = 3
requires-python = ">=3.12,<3.13"

[[package]]
name = "torch"
version = "2.12.1+cpu"

[[package]]
name = "torch"
version = "2.13.0"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="one public locked version"):
        locked_public_version(lock, "torch")


def test_commands_keep_frozen_export_and_fail_closed_public_audit(
    tmp_path: Path,
) -> None:
    export = export_command(
        "uv",
        tmp_path / "project",
        tmp_path / "requirements.txt",
        ("torch",),
    )
    audit = audit_command(tmp_path / "torch.txt", no_deps=True)

    assert "--frozen" in export
    assert export[export.index("--no-emit-package") + 1] == "torch"
    assert audit[:5] == (sys.executable, "-m", "pip_audit", "--strict", "--no-deps")
    assert "--disable-pip" in audit


def test_full_verify_audits_every_isolated_python_runtime_lock() -> None:
    commands = profile_audit_commands("python")
    projects = {command[command.index("--project") + 1] for command in commands}

    assert projects == {
        "sidecars/lean",
        "sidecars/nautilus",
        "sidecars/openbb",
        "workers/kronos",
        "workers/kronos/profiles/cuda",
        "workers/quant_lab",
        "workers/quant_lab/rd_agent",
        "workers/tradingagents",
    }
    kronos = tuple(
        command
        for command in commands
        if command[command.index("--project") + 1].startswith("workers/kronos")
    )
    assert len(kronos) == 2
    assert all(
        command[-2:] == ("--standard-identity-package", "torch") for command in kronos
    )
