from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_secrets.py"


def run_scan(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_repository_contains_no_high_confidence_secrets() -> None:
    result = run_scan(ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert '"success": true' in result.stdout


def test_scan_rejects_openai_key_and_never_echoes_secret(tmp_path: Path) -> None:
    secret = "sk-proj-" + "a" * 48
    (tmp_path / "leak.py").write_text(
        f'OPENAI_API_KEY = "{secret}"\n', encoding="utf-8"
    )

    result = run_scan(tmp_path)

    assert result.returncode == 1
    assert "OPENAI_API_KEY" in result.stdout
    assert secret not in result.stdout


def test_scan_rejects_private_key_header(tmp_path: Path) -> None:
    (tmp_path / "private.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\nredacted\n",  # pragma: allowlist secret
        encoding="utf-8",
    )

    result = run_scan(tmp_path)

    assert result.returncode == 1
    assert "PRIVATE_KEY" in result.stdout


def test_scan_ignores_research_runtime_data_and_virtual_environment(
    tmp_path: Path,
) -> None:
    secret = "ghp_" + "b" * 40
    for directory in (".data", ".research", ".venv"):
        path = tmp_path / directory
        path.mkdir()
        (path / "ignored.txt").write_text(secret, encoding="utf-8")

    result = run_scan(tmp_path)

    assert result.returncode == 0
