from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_openbb_sidecar.py"


def _copy_policy_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative in (
        "config/providers/default.yaml",
        "infra/compose.openbb.yaml",
        "src/stonks_agent/adapters/market_data/openbb_rest.py",
        "pyproject.toml",
        "uv.lock",
        "THIRD_PARTY_NOTICES.md",
    ):
        source = PROJECT_ROOT / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copytree(
        PROJECT_ROOT / "sidecars" / "openbb",
        root / "sidecars" / "openbb",
        ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache"),
    )
    return root


def _run_policy(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(POLICY_SCRIPT), "--root", str(root)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _rewrite(path: Path, transform: Callable[[str], str]) -> None:
    original = path.read_text(encoding="utf-8")
    updated = transform(original)
    assert updated != original
    path.write_text(updated, encoding="utf-8")


def _failure_codes(result: subprocess.CompletedProcess[str]) -> set[str]:
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert payload["status"] == "failed"
    assert payload["data"] is None
    assert payload["error"]["code"] == "OPENBB_SIDECAR_POLICY_VIOLATION"
    return {item["code"] for item in payload["error"]["details"]}


def test_repository_satisfies_openbb_sidecar_policy() -> None:
    result = _run_policy(PROJECT_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "data": {"check_count": 8, "violation_count": 0},
        "error": None,
        "status": "passed",
        "success": True,
    }


def test_cli_emits_structured_fail_closed_input_error(tmp_path: Path) -> None:
    result = _run_policy(tmp_path / "missing")

    assert result.returncode == 1
    codes = _failure_codes(result)
    assert codes == {"POLICY_INPUT_ERROR"}
    assert result.stderr == ""


def test_ci_runs_static_gates_before_live_sidecar_and_always_cleans_up() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    ordered_tokens = (
        "uv run python scripts/verify_openbb_sidecar.py",
        "uv lock --check --project sidecars/openbb",
        "docker compose -f infra/compose.openbb.yaml config --quiet",
        "build --pull openbb",
        "up --detach --wait",
        "http://127.0.0.1:6900/healthz",
        "http://127.0.0.1:6900/source",
        "if: always()",
        "--volumes --remove-orphans",
    )
    positions = [workflow.index(token) for token in ordered_tokens]
    assert positions == sorted(positions)


def test_policy_rejects_openbb_in_core_lock(tmp_path: Path) -> None:
    root = _copy_policy_fixture(tmp_path)
    lock = root / "uv.lock"
    _rewrite(
        lock,
        lambda value: value
        + '\n[[package]]\nname = "openbb-core"\nversion = "1.6.13"\n'
        + 'source = { registry = "https://pypi.org/simple" }\n',
    )

    result = _run_policy(root)

    assert result.returncode == 1
    assert "OPENBB_IN_CORE_LOCK" in _failure_codes(result)


def test_policy_rejects_missing_locked_component_from_sbom(tmp_path: Path) -> None:
    root = _copy_policy_fixture(tmp_path)
    sbom_path = root / "sidecars" / "openbb" / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    original_count = len(sbom["components"])
    sbom["components"] = [
        component
        for component in sbom["components"]
        if component["name"].lower() != "pyjwt"
    ]
    assert len(sbom["components"]) == original_count - 1
    sbom_path.write_text(json.dumps(sbom), encoding="utf-8")

    result = _run_policy(root)

    assert result.returncode == 1
    assert "SBOM_MISSING_LOCK_COMPONENT" in _failure_codes(result)


@pytest.mark.parametrize(
    ("relative", "transform", "expected_code"),
    [
        (
            "sidecars/openbb/Dockerfile",
            lambda value: re.sub(
                r"(@sha256:)[0-9a-f]{64}", r"\1latest", value, count=1
            ),
            "UNPINNED_DOCKER_BASE",
        ),
        (
            "sidecars/openbb/Dockerfile",
            lambda value: value.replace("uv sync --frozen", "uv sync"),
            "NON_FROZEN_SIDECAR_BUILD",
        ),
        (
            "sidecars/openbb/Dockerfile",
            lambda value: value.replace(
                "--checksum=sha256:c26cfc2ae37c1700e01db9ca0fd2cd02118715cec2097ea870f47a70186402bb",
                "--checksum=sha256:" + "0" * 64,
            ),
            "SOURCE_ARCHIVE_NOT_EMBEDDED",
        ),
        (
            "sidecars/openbb/app.py",
            lambda value: value.replace('"/source"', '"/source-disabled"'),
            "MISSING_SOURCE_ROUTE",
        ),
        (
            "infra/compose.openbb.yaml",
            lambda value: value.replace('"127.0.0.1:6900:6900"', '"0.0.0.0:6900:6900"'),
            "NON_LOOPBACK_BIND",
        ),
        (
            "sidecars/openbb/provider-manifest.yaml",
            lambda value: value.replace(
                "origin: http://127.0.0.1:6900",
                "origin: http://127.0.0.1:6901",
            ),
            "TRANSPORT_POLICY_DRIFT",
        ),
        (
            "sidecars/openbb/NOTICE.md",
            lambda value: value.replace("GET /source", "source unavailable"),
            "INCOMPLETE_LEGAL_NOTICE",
        ),
        (
            "sidecars/openbb/SOURCE_OFFER.md",
            lambda value: value.replace(
                "c26cfc2ae37c1700e01db9ca0fd2cd02118715cec2097ea870f47a70186402bb",
                "missing-hash",
            ),
            "INCOMPLETE_SOURCE_OFFER",
        ),
    ],
)
def test_policy_rejects_security_and_source_offer_drift(
    tmp_path: Path,
    relative: str,
    transform: Callable[[str], str],
    expected_code: str,
) -> None:
    root = _copy_policy_fixture(tmp_path)
    _rewrite(root / relative, transform)

    result = _run_policy(root)

    assert result.returncode == 1
    assert expected_code in _failure_codes(result)
