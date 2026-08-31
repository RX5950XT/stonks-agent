from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "start.ps1"
WINDOWS_WRAPPER = ROOT / "start.cmd"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@pytest.fixture
def kronos_model_directory() -> Iterator[None]:
    paths = (
        ROOT / ".data",
        ROOT / ".data" / "models",
        ROOT / ".data" / "models" / "kronos",
    )
    created = tuple(path for path in paths if not path.exists())
    paths[-1].mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        for path in reversed(created):
            path.rmdir()


def _run_script(
    *arguments: str,
    environment: dict[str, str] | None = None,
    script: Path = SCRIPT,
    root: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    if POWERSHELL is None:
        pytest.skip("PowerShell is unavailable")
    return subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )


def _copy_launcher_checkout(root: Path) -> Path:
    (root / "infra").mkdir(parents=True)
    (root / "workers" / "kronos").mkdir(parents=True)
    for source, target in (
        (SCRIPT, root / "start.ps1"),
        (ROOT / "pyproject.toml", root / "pyproject.toml"),
        (ROOT / "infra" / "compose.gui.yaml", root / "infra" / "compose.gui.yaml"),
        (
            ROOT / "infra" / "compose.kronos.yaml",
            root / "infra" / "compose.kronos.yaml",
        ),
        (
            ROOT / "workers" / "kronos" / "model-manifest.json",
            root / "workers" / "kronos" / "model-manifest.json",
        ),
    ):
        shutil.copy2(source, target)
    return root / "start.ps1"


def test_market_check_reports_exact_non_mutating_command() -> None:
    result = _run_script(
        "-Mode",
        "market",
        "-Check",
        "-NoBrowser",
        "-Port",
        "8877",
    )

    assert result.returncode == 0, result.stderr
    assert "mode=market" in result.stdout
    assert (
        "uv run --frozen stonks-gui serve --port 8877 --no-open-browser"
        in result.stdout
    )
    assert "--with-paper" not in result.stdout
    assert "--with-research" not in result.stdout


def test_research_check_allows_model_configuration_in_gui(
    kronos_model_directory: None,
) -> None:
    environment = os.environ.copy()
    for name in (
        "STONKS_LLM_BASE_URL",
        "STONKS_LLM_MODEL",
        "STONKS_LLM_API_KEY",
    ):
        environment.pop(name, None)

    result = _run_script("-Mode", "research", "-Check", environment=environment)

    assert result.returncode == 0, result.stderr
    assert "mode=research" in result.stdout
    assert "--with-research" in result.stdout
    assert "STONKS_LLM" not in result.stdout + result.stderr


def test_research_check_fails_closed_without_kronos_model(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    root = tmp_path / "checkout"
    script = _copy_launcher_checkout(root)

    result = _run_script(
        "-Mode",
        "research",
        "-Check",
        script=script,
        root=root,
    )

    assert result.returncode == 2
    assert "Kronos CPU model is missing" in result.stderr
    assert "fetch_kronos_model.py" in result.stderr


def test_research_check_never_prints_api_key(
    kronos_model_directory: None,
) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "STONKS_ENVIRONMENT": "local",
            "STONKS_LLM_BASE_URL": "http://127.0.0.1:11434",
            "STONKS_LLM_MODEL": "local-model",
            "STONKS_LLM_API_KEY": "super-secret-test-value",
        }
    )

    result = _run_script(
        "-Mode",
        "research",
        "-Check",
        "-DatabasePort",
        "55444",
        "-KronosPort",
        "17244",
        environment=environment,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, result.stderr
    assert "mode=research" in result.stdout
    assert "--with-research" in result.stdout
    assert "--database-port 55444" in result.stdout
    assert "--kronos-port 17244" in result.stdout
    assert "super-secret-test-value" not in combined


def test_script_has_safe_defaults_and_stays_in_source_checkout() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert source.isascii()
    assert '[ValidateSet("market", "paper", "research")]' in source
    assert '[string]$Mode = "research"' in source
    assert "Join-Path $PSScriptRoot" in source
    assert "uv sync --frozen" in source
    assert "docker system prune" not in source
    assert "Remove-Item" not in source
    assert "Invoke-Expression" not in source
    assert "STONKS_LLM_API_KEY =" not in source


def test_windows_wrapper_delegates_to_research_launcher() -> None:
    source = WINDOWS_WRAPPER.read_text(encoding="utf-8")

    assert source == (
        "@echo off\n"
        "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "
        '"%~dp0start.ps1" -Mode research -DatabasePort 55434\n'
        "if errorlevel 1 pause\n"
    )


def test_local_environment_file_is_key_scoped_and_never_echoed() -> None:
    """`.env` is a local env source only: STONKS_* keys, never printed."""

    source = SCRIPT.read_text(encoding="utf-8")

    assert 'Join-Path $PSScriptRoot ".env"' in source
    assert '$name -notmatch "^STONKS_[A-Z0-9_]+$"' in source
    assert "Write-Output $value" not in source
    assert "Write-Output $entry" not in source


def test_local_environment_file_rejects_foreign_keys(tmp_path: Path) -> None:
    if POWERSHELL is None:
        pytest.skip("PowerShell is unavailable")
    env_file = ROOT / ".env"
    if env_file.exists():
        pytest.skip("a local .env is already present")

    env_file.write_text("PATH=/evil\n", encoding="utf-8")
    try:
        result = _run_script("-Mode", "market", "-Check")
    finally:
        env_file.unlink()

    assert result.returncode == 1
    assert "only accepts STONKS_* keys" in result.stderr


def test_local_environment_file_never_prints_its_secret(
    kronos_model_directory: None,
) -> None:
    if POWERSHELL is None:
        pytest.skip("PowerShell is unavailable")
    env_file = ROOT / ".env"
    if env_file.exists():
        pytest.skip("a local .env is already present")

    env_file.write_text(
        "STONKS_LLM_MODEL=local-model\nSTONKS_LLM_API_KEY=super-secret-test-value\n",
        encoding="utf-8",
    )
    try:
        result = _run_script("-Mode", "research", "-Check")
    finally:
        env_file.unlink()

    combined = result.stdout + result.stderr
    assert result.returncode == 0, result.stderr
    assert "mode=research" in result.stdout
    assert "super-secret-test-value" not in combined


def test_shell_launcher_mirrors_the_powershell_policy() -> None:
    source = (ROOT / "start.sh").read_text(encoding="utf-8")

    assert source.isascii()
    assert source.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in source
    assert 'MODE="research"' in source
    assert "market|paper|research) ;;" in source
    assert "uv sync --frozen" in source
    assert "docker system prune" not in source
    assert "rm -rf" not in source
    assert "eval " not in source
    assert "^STONKS_[A-Z0-9_]+$" in source


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("market", "uv run --frozen stonks-gui serve --port 8787"),
        (
            "paper",
            "uv run --frozen stonks-gui serve --port 8787"
            " --with-paper --database-port 55433",
        ),
        (
            "research",
            "uv run --frozen stonks-gui serve --port 8787"
            " --with-research --database-port 55433 --kronos-port 17200",
        ),
    ],
)
def test_shell_launcher_check_matches_powershell(
    mode: str,
    expected: str,
    kronos_model_directory: None,
) -> None:
    bash = shutil.which("bash")
    if bash is None or shutil.which("uv") is None or shutil.which("docker") is None:
        pytest.skip("bash, uv, or docker is unavailable")

    result = subprocess.run(
        [bash, "./start.sh", "--mode", mode, "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )

    if result.returncode != 0:
        pytest.skip(f"shell launcher preconditions unmet: {result.stderr.strip()}")
    assert f"mode={mode}" in result.stdout
    assert expected in result.stdout
