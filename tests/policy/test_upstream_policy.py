from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_SCRIPT = PROJECT_ROOT / "scripts" / "check_upstream_policy.py"

SNAPSHOTS = {
    "ai-hedge-fund": "3a18702cb25777fb4bdb4b2527a0c868bc8297f4",
    "dexter": "bae661670c3d77e909942777ac32ece21e8af35d",
    "tradingagents": "01477f9afb7a47b849ed4c9259d3a9a4738d9fda",
    "kronos": "67b630e67f6a18c9e9be918d9b4337c960db1e9a",
    "daily-stock-analysis": "aa513135d67425d2484cdc9c643402c0f4c3ae07",
    "ai-trader": "d03ff6c056b32ced735adf7c19ed8175adb1c8df",
    "openbb": "1c74893140292944e71ff5cdd9536edf12f05483",
    "nautilus-trader": "8160730c7c550480b0a439fb11086a4c4de15f0b",
    "qlib": "d5379c520f66a39953bad76234a7019a72796fd0",
    "rd-agent": "4f9ecb005881cddc08df0124a2e894c018007679",
}


def _entry(upstream_id: str, snapshot: str) -> dict[str, Any]:
    is_restricted = upstream_id in {"dexter", "ai-trader"}
    is_openbb = upstream_id == "openbb"
    is_nautilus = upstream_id == "nautilus-trader"
    expression = "NOASSERTION" if is_restricted else "MIT"
    if is_openbb:
        expression = "AGPL-3.0-only"
    if is_nautilus:
        expression = "LGPL-3.0-or-later"
    entry = {
        "id": upstream_id,
        "repository": f"https://github.com/example/{upstream_id}",
        "local_path": f".research/upstreams/{upstream_id}",
        "snapshot": snapshot,
        "license": {
            "expression": expression,
            "status": "incomplete" if is_restricted else "verified",
            "evidence": [
                {"path": "LICENSE", "sha256": "0" * 64},
            ],
        },
        "adoption": {
            "mode": (
                "clean-room-only"
                if is_restricted
                else "optional-sidecar"
                if is_openbb or is_nautilus
                else "research-only"
            ),
            "source_copy_allowed": False,
            "in_core_allowed": False,
        },
        "notice": {"required": False, "id": None},
    }
    if is_nautilus:
        entry["repository"] = "https://github.com/nautechsystems/nautilus_trader"
        entry["notice"] = {
            "required": True,
            "id": "NAUTILUS-TRADER-LGPL-3.0-SIDECAR",
        }
    return entry


def _valid_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "reviewed_at": "2026-07-10",
        "policies": {
            "required_gates": [
                "NO_VENDOR_DEXTER_CODE",
                "NO_VENDOR_AI_TRADER_CODE",
                "NO_OPENBB_IMPORT_IN_CORE",
            ],
            "forbidden_core_dependencies": [
                "langgraph",
                "lean",
                "nautilus-trader",
                "openbb",
                "openbb-core",
                "openbb-platform",
                "openbb-terminal",
                "pyqlib",
                "pytorch",
                "qlib",
                "rd-agent",
                "rdagent",
                "torch",
            ],
            "forbidden_vendor_roots": [
                "packages/dexter",
                "src/dexter",
                "third_party/dexter",
                "vendor/dexter",
                "workers/dexter",
                "packages/ai-trader",
                "src/ai-trader",
                "third_party/ai-trader",
                "vendor/ai-trader",
                "workers/ai-trader",
            ],
        },
        "upstreams": [
            _entry(upstream_id, snapshot) for upstream_id, snapshot in SNAPSHOTS.items()
        ],
    }


def _write_fixture_repo(
    root: Path,
    *,
    manifest: dict[str, Any] | None = None,
    dependencies: tuple[str, ...] = ("pydantic>=2",),
    notices: str = (
        "# Third-party notices\n"
        "NAUTILUS-TRADER-LGPL-3.0-SIDECAR\n"
        "https://github.com/nautechsystems/nautilus_trader\n"
        "8160730c7c550480b0a439fb11086a4c4de15f0b\n"
        "Copyright (C) 2015-2026 Nautech Systems Pty Ltd\n"
    ),
) -> Path:
    (root / "docs" / "legal").mkdir(parents=True)
    (root / "src" / "stonks_agent").mkdir(parents=True)
    (root / "packages" / "contracts" / "src").mkdir(parents=True)
    (root / "docs" / "legal" / "upstream-manifest.yaml").write_text(
        json.dumps(manifest or _valid_manifest()), encoding="utf-8"
    )
    dependency_lines = ",\n".join(f'    "{item}"' for item in dependencies)
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "fixture"\n'
        'version = "0.0.0"\n'
        "dependencies = [\n"
        f"{dependency_lines}\n"
        "]\n",
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )
    (root / "THIRD_PARTY_NOTICES.md").write_text(notices, encoding="utf-8")
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


def test_repository_satisfies_upstream_policy() -> None:
    result = _run_policy(PROJECT_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert '"success": true' in result.stdout


def test_policy_rejects_openbb_core_dependency(tmp_path: Path) -> None:
    root = _write_fixture_repo(tmp_path, dependencies=("openbb>=4",))

    result = _run_policy(root)

    assert result.returncode == 1
    assert "FORBIDDEN_CORE_DEPENDENCY" in result.stdout


def test_policy_rejects_openbb_core_import(tmp_path: Path) -> None:
    root = _write_fixture_repo(tmp_path)
    (root / "src" / "stonks_agent" / "provider.py").write_text(
        "from openbb import obb\n", encoding="utf-8"
    )

    result = _run_policy(root)

    assert result.returncode == 1
    assert "NO_OPENBB_IMPORT_IN_CORE" in result.stdout


def test_policy_rejects_dexter_vendor_tree(tmp_path: Path) -> None:
    root = _write_fixture_repo(tmp_path)
    (root / "vendor" / "dexter").mkdir(parents=True)
    (root / "vendor" / "dexter" / "copied.py").write_text(
        "print('copied')\n", encoding="utf-8"
    )

    result = _run_policy(root)

    assert result.returncode == 1
    assert "NO_VENDOR_DEXTER_CODE" in result.stdout


def test_policy_rejects_ai_trader_vendor_tree_case_insensitively(
    tmp_path: Path,
) -> None:
    root = _write_fixture_repo(tmp_path)
    (root / "third_party" / "AI-Trader").mkdir(parents=True)
    (root / "third_party" / "AI-Trader" / "copied.py").write_text(
        "print('copied')\n", encoding="utf-8"
    )

    result = _run_policy(root)

    assert result.returncode == 1
    assert "NO_VENDOR_AI_TRADER_CODE" in result.stdout


def test_policy_rejects_unknown_license(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["upstreams"][0]["license"]["status"] = "unknown"
    root = _write_fixture_repo(tmp_path, manifest=manifest)

    result = _run_policy(root)

    assert result.returncode == 1
    assert "UNKNOWN_LICENSE_STATUS" in result.stdout


def test_policy_rejects_missing_required_notice(tmp_path: Path) -> None:
    manifest = deepcopy(_valid_manifest())
    manifest["upstreams"][0]["notice"] = {
        "required": True,
        "id": "ai-hedge-fund@3a18702c",
    }
    root = _write_fixture_repo(tmp_path, manifest=manifest)

    result = _run_policy(root)

    assert result.returncode == 1
    assert "MISSING_REQUIRED_NOTICE" in result.stdout


def test_policy_requires_all_critical_gates(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["policies"]["required_gates"].remove("NO_OPENBB_IMPORT_IN_CORE")
    root = _write_fixture_repo(tmp_path, manifest=manifest)

    result = _run_policy(root)

    assert result.returncode == 1
    assert "MISSING_CRITICAL_GATE" in result.stdout
